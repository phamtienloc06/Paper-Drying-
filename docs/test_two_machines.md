# Test HMI và SCADA qua ThingsBoard khi chưa có phần cứng

Máy 1 chạy HMI với nhiệt độ và relay mô phỏng. Máy 2 chạy SCADA. Hai máy trao
đổi qua ThingsBoard; không cần nối COM, PT100, relay hoặc nối trực tiếp hai máy.
Cả hai máy cần Internet và chạy cùng bản code có hỗ trợ `--mqtt`.

```text
HMI + mô phỏng máy 1 <-> MQTT/TLS <-> ThingsBoard <-> REST/WebSocket <-> SCADA máy 2
```

## 1. Chuẩn bị một Device trên ThingsBoard

Dùng một Device thử nghiệm. Token trên máy 1 và UUID trên máy 2 phải thuộc cùng
Device này. Tài khoản ThingsBoard dùng ở máy 2 cần quyền đọc telemetry và gửi RPC
đến Device đó.

## 2. Máy 1: HMI mô phỏng

Từ thư mục gốc dự án:

```powershell
python -m pip install -r control_station/requirements.txt
if (!(Test-Path control_station/.env)) {
    Copy-Item control_station/.env.example control_station/.env
}
```

Điền thông tin vào `control_station/.env`:

```dotenv
TB_DEVICE_TOKEN=TOKEN_CUA_DEVICE_THU_NGHIEM
TB_MQTT_HOST=mqtt.thingsboard.cloud
TB_MQTT_PORT=8883
```

Host trên là cấu hình mẫu đang có trong dự án; dùng host MQTT của instance
ThingsBoard tương ứng nếu bạn dùng instance khác.

```powershell
python -m control_station.main --mqtt
```

Mở HMI trên máy 1 tại `http://127.0.0.1:8000` theo cấu hình YAML hiện tại.
Không thêm `--hardware`: nhiệt độ và relay sẽ chạy trong RAM, không mở COM.
Log `Connected to ThingsBoard MQTT.` cho biết kết nối đã thành công.
Trong ThingsBoard, Device phải có telemetry `simulation=true` và nhiệt độ mới.

## 3. Máy 2: SCADA

Từ thư mục gốc của cùng bản dự án trên máy 2:

```powershell
python -m pip install -r remote_scada/requirements.txt
if (!(Test-Path remote_scada/.env)) {
    Copy-Item remote_scada/.env.example remote_scada/.env
}
```

Điền vào `remote_scada/.env`:

| Biến | Giá trị |
|---|---|
| `TB_BASE_URL` | URL HTTPS của instance ThingsBoard |
| `TB_USERNAME`, `TB_PASSWORD` | Tài khoản ThingsBoard có quyền với Device thử nghiệm |
| `TB_DEVICE_ID` | UUID của chính Device đang dùng trên máy 1 |
| `SCADA_USERNAME`, `SCADA_PASSWORD` | Tài khoản đăng nhập web SCADA do bạn đặt |
| `SCADA_SECRET_KEY` | Chuỗi ngẫu nhiên riêng, ít nhất 32 ký tự |

Có thể tạo secret key bằng `python -c "import secrets; print(secrets.token_urlsafe(32))"`
rồi điền kết quả trực tiếp vào file `.env`. Giữ host/port SCADA mặc định để mở
web ngay trên máy 2. Không chép Device Token máy 1 vào cấu hình máy 2.

```powershell
python -m remote_scada.server
```

Trên máy 2 mở `http://127.0.0.1:8080`, đăng nhập bằng tài khoản SCADA vừa đặt.
SCADA phải hiển thị nhãn **MÔ PHỎNG** và dữ liệu giống HMI máy 1.
`127.0.0.1` ở mỗi máy chỉ truy cập ứng dụng chạy trên chính máy đó; dữ liệu giữa
hai máy đi qua ThingsBoard, không cần đổi địa chỉ web thành IP của máy còn lại.

## 4. Bài thử

1. Khi đang OFF, đặt 35 °C, thời gian giữ nhiệt 0.1 phút, ngưỡng nguội 32 °C
   trên SCADA. HMI máy 1 phải nhận cùng thông số. 0.1 phút tương đương 6 giây
   giữ nhiệt đủ điều kiện; thời gian gia nhiệt và làm nguội không nằm trong 6 giây đó.
2. Bấm **Bắt đầu sấy** trên SCADA: cả hai giao diện phải chuyển sang AUTO,
   relay gia nhiệt mô phỏng bật và nhiệt độ tăng.
3. Theo dõi HEATING -> HOLDING -> COOLING -> COMPLETE. Đợi đủ thời gian thực
   để mô hình gia nhiệt, giữ nhiệt và làm nguội; thử nghiệm không thay đồng hồ controller.
4. Bắt đầu lại rồi bấm **Chuyển OFF** trên SCADA: HMI về OFF, hai đầu ra mô phỏng tắt.
5. Thử **Dừng khẩn cấp**: cả hai giao diện thấy FAULT. Reset trên HMI máy 1;
   quyền reset từ xa vẫn bị giới hạn như khi dùng phần cứng.

Chỉ chạy `python -m control_station.main` thì HMI mô phỏng hoạt động cục bộ,
chưa kết nối ThingsBoard. Hãy dùng `--mqtt` khi cần test cùng SCADA.
Các bài test tự động dùng đường truyền ThingsBoard giả để kiểm tra telemetry,
RPC và đồng bộ hai API. Kết nối Internet, tài khoản và quyền Device cần được
xác nhận bằng lần chạy thực tế với cấu hình riêng của bạn.
