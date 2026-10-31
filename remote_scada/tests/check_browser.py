"""Kiểm tra SCADA bằng Chrome/Edge headless và controller mô phỏng, không COM/cloud.

    python -m remote_scada.tests.check_browser

Không cần Selenium/Playwright. Dùng Chrome DevTools Protocol trên cổng localhost
tạm thời; profile trình duyệt tạm được đóng và dọn sau khi kiểm tra.
"""

import base64
import csv
import json
import logging
from pathlib import Path
import subprocess
import tempfile
import threading
import time

import requests
import websocket
from werkzeug.serving import make_server

from remote_scada.preview import create_preview_app


class Browser:
    def __init__(self, connection):
        self.connection = connection
        self.sequence = 0
        self.errors = []

    def call(self, method, **params):
        self.sequence += 1
        number = self.sequence
        self.connection.send(json.dumps({"id": number, "method": method, "params": params}))
        while True:
            result = json.loads(self.connection.recv())
            if result.get("method") == "Runtime.exceptionThrown":
                self.errors.append(result["params"]["exceptionDetails"])
            if result.get("id") == number:
                if "error" in result:
                    raise AssertionError(result["error"])
                return result.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression,
                           returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def wait(self, expression, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(0.05)
        raise AssertionError("Timed out waiting for " + expression)

    def screenshot(self, path):
        result = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        path.write_bytes(base64.b64decode(result["data"]))


def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    candidates = [Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                  Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise RuntimeError("Cần Chrome hoặc Edge để chạy kiểm tra trình duyệt.")
    output = Path("artifacts/scada")
    output.mkdir(parents=True, exist_ok=True)
    app = create_preview_app()
    plant = app.extensions["preview_plant"]
    gateway = app.extensions["thingsboard_client"]
    rpc_calls = []
    original_rpc = gateway.send_rpc

    def record_rpc(method, params):
        rpc_calls.append(method)
        return original_rpc(method, params)

    gateway.send_rpc = record_rpc
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = connection = None
    try:
        with tempfile.TemporaryDirectory(prefix="scada-browser-") as folder:
            profile = Path(folder).resolve()
            process = subprocess.Popen([
                str(executable), "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", "--remote-debugging-port=0",
                "--user-data-dir=" + str(profile),
                "http://127.0.0.1:" + str(server.server_port),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                port_file = profile / "DevToolsActivePort"
                deadline = time.monotonic() + 15
                while not port_file.exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError("Trình duyệt không mở DevTools.")
                    time.sleep(0.05)
                port = port_file.read_text().splitlines()[0]
                pages = requests.get("http://127.0.0.1:" + port + "/json", timeout=5).json()
                target = next(page for page in pages if page["type"] == "page")
                connection = websocket.create_connection(target["webSocketDebuggerUrl"],
                                                        timeout=10, suppress_origin=True)
                browser = Browser(connection)
                browser.call("Runtime.enable")
                browser.call("Page.enable")
                browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1100,
                             deviceScaleFactor=1, mobile=False)
                browser.wait("Boolean(document.getElementById('username'))")
                browser.evaluate("document.getElementById('username').value='operator'; document.getElementById('password').value='demo-scada'; document.querySelector('form').requestSubmit(); true")
                browser.wait("typeof isFresh === 'function' && isFresh() && !document.getElementById('start-button').disabled")
                assert browser.evaluate("document.querySelectorAll('[data-panel]:not([hidden])').length") == 1
                # Bản nhập phải tồn tại qua nhiều lần telemetry cập nhật, kể cả sau blur.
                browser.evaluate("const input=document.getElementById('setpoint-input'); input.focus(); input.value='45'; input.dispatchEvent(new Event('input',{bubbles:true})); input.blur(); true")
                for _ in range(3):
                    browser.evaluate("refreshState()")
                assert browser.evaluate("document.getElementById('setpoint-input').value") == "45"
                assert browser.evaluate("document.getElementById('start-button').disabled")
                browser.evaluate("[['setpoint-input','35'],['duration-input','0.1'],['cooling-input','34']].forEach(([id,value])=>{const node=document.getElementById(id);node.value=value;node.dispatchEvent(new Event('input',{bubbles:true}));}); document.getElementById('settings-form').requestSubmit(); true")
                assert browser.evaluate("document.getElementById('command-result').textContent.includes('deadband')")
                assert rpc_calls == []
                # Gửi bộ thông số không thể áp dụng tuần tự bằng ba lệnh cũ.
                browser.evaluate("[['setpoint-input','35'],['duration-input','0.1'],['cooling-input','32']].forEach(([id,value])=>{const node=document.getElementById(id);node.value=value;node.dispatchEvent(new Event('input',{bubbles:true}));}); document.getElementById('settings-form').requestSubmit(); true")
                browser.wait("!pendingRecipe && dirtyFields.size===0 && document.getElementById('setpoint-input').value==='35'")
                assert rpc_calls.count("SET_RECIPE") == 1
                assert plant.controller.get_settings_snapshot().temperature_setpoint_c == 35
                browser.evaluate("document.getElementById('start-button').click(); document.getElementById('start-button').click(); true")
                browser.wait("latestPayload.state.mode==='AUTO' && latestPayload.state.auto_phase==='HEATING' && latestPayload.state.heater_on===true && !commandBusy")
                assert rpc_calls.count("START_DRYING") == 1
                assert browser.evaluate("document.getElementById('setpoint-input').disabled")
                browser.screenshot(output / "operation.png")
                print("PASS: login, dirty form, atomic recipe, duplicate click and active-batch lock", flush=True)
                # Đọc lịch sử chậm không khóa cập nhật trạng thái.
                browser.evaluate("window.__realFetch=window.fetch;window.__stateReads=0;window.fetch=(url,options)=>{if(url==='/api/state')window.__stateReads++;if(String(url).startsWith('/api/history'))return new Promise((resolve,reject)=>{window.__releaseHistory=()=>window.__realFetch(url,options).then(resolve,reject);options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')));});return window.__realFetch(url,options);};location.hash='trends';true")
                browser.wait("historyRefreshRunning && typeof window.__releaseHistory==='function'")
                for _ in range(3):
                    browser.evaluate("refreshState()")
                assert browser.evaluate("window.__stateReads") >= 3
                browser.evaluate("window.fetch=window.__realFetch;window.__releaseHistory();true")
                browser.wait("historyData !== null && !historyRefreshRunning")
                browser.screenshot(output / "trends.png")
                assert browser.evaluate("!document.getElementById('export-history').disabled")
                browser.call("Browser.setDownloadBehavior", behavior="allow", downloadPath=str(output.resolve()))
                csv_path = output / "scada-nhiet-do.csv"
                if csv_path.exists():
                    csv_path.unlink()
                browser.evaluate("document.getElementById('export-history').click();true")
                deadline = time.monotonic() + 5
                while not csv_path.exists():
                    if time.monotonic() > deadline:
                        raise AssertionError("CSV was not downloaded")
                    time.sleep(0.05)
                with csv_path.open(encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                assert rows and rows[0]["timestamp_utc"].endswith("Z")
                assert any(row["temperature_c"] for row in rows)
                assert browser.evaluate("validSeries([{ts:1,value:null},{ts:2,value:0}]).length") == 1
                print("PASS: invalid recipe blocked in UI and numeric history exported as UTC CSV", flush=True)
                # Dữ liệu cũ không được giữ đầu ra/đèn kết nối như đang có xác nhận mới.
                browser.evaluate("window.fetch=(url,options)=>url==='/api/state'?Promise.resolve(new Response(JSON.stringify({...latestPayload,telemetry_age_s:30}),{status:200,headers:{'Content-Type':'application/json'}})):window.__realFetch(url,options);location.hash='operation';true")
                browser.evaluate("refreshState()")
                browser.wait("!isFresh() && !document.getElementById('data-banner').hidden")
                assert browser.evaluate("document.getElementById('temperature').textContent") == "—"
                assert browser.evaluate("document.getElementById('heater-feedback').textContent") == "Chưa xác nhận"
                assert browser.evaluate("document.getElementById('start-button').disabled && !document.getElementById('emergency-button').disabled")
                browser.screenshot(output / "stale.png")
                browser.evaluate("window.fetch=(url,options)=>url==='/api/state'?Promise.reject(new Error('offline test')):window.__realFetch(url,options);true")
                browser.evaluate("refreshState()")
                assert browser.evaluate("!backendAvailable && document.getElementById('setpoint-input').disabled")
                browser.evaluate("window.fetch=window.__realFetch;true")
                browser.evaluate("refreshState()")
                browser.wait("isFresh()")
                print("PASS: independent history polling, stale data, API failure and recovery", flush=True)
                browser.evaluate("window.confirm=()=>true;document.getElementById('emergency-button').click();true")
                browser.wait("latestPayload.state.mode==='FAULT' && !emergencyBusy")
                assert not plant.relay.heater_on and not plant.relay.process_fan_on
                browser.evaluate("location.hash='alarms';true")
                browser.wait("activeView==='alarms'")
                browser.evaluate("refreshHistory()")
                assert browser.evaluate("document.getElementById('active-alarm').textContent.includes('dừng khẩn')")
                browser.screenshot(output / "alarms.png")
                browser.evaluate("location.hash='diagnostics';true")
                browser.wait("activeView==='diagnostics'")
                browser.screenshot(output / "diagnostics.png")
                browser.call("Emulation.setDeviceMetricsOverride", width=390, height=844,
                             deviceScaleFactor=1, mobile=True)
                browser.evaluate("location.hash='operation';true")
                browser.wait("activeView==='operation'")
                assert browser.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")
                assert browser.evaluate("getComputedStyle(document.getElementById('source-label')).display!=='none'")
                browser.screenshot(output / "mobile.png")
                assert browser.errors == [], browser.errors
                print("PASS: emergency request, alarm history, four views, mobile width and no JavaScript exceptions", flush=True)
                print("Screenshots: " + str(output.resolve()), flush=True)
            finally:
                if connection:
                    connection.close()
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        plant.stop()


if __name__ == "__main__":
    main()
