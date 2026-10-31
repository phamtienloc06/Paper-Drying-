# Hệ thống điều khiển buồng sấy giấy

Máy 1 có Modbus RTU, driver PT100/relay, controller, HMI Flask, mô phỏng và
vòng chạy phần cứng. Cầu nối MQTT/ThingsBoard và SCADA máy 2 đã được tích hợp;
lịch sử telemetry/alarm được lưu trên ThingsBoard, chưa có database cục bộ riêng.

## Chạy máy 1

Từ thư mục gốc repository:

```powershell
python -m pip install -r control_station/requirements.txt
python -m control_station.main
```

Mặc định chạy mô phỏng và không mở COM. Mở HMI ở địa chỉ trong
`control_station/config.yaml` (hiện là http://127.0.0.1:8000).
`python -m control_station.hmi.app` dùng cùng entry point và cấu hình.

Để test HMI và SCADA qua ThingsBoard khi chưa có PT100/relay thật:

```powershell
python -m control_station.main --mqtt
```

Chế độ này mô phỏng nhiệt và relay trong RAM, không mở COM; cần điền
`TB_DEVICE_TOKEN` trong `control_station/.env`. SCADA hiển thị nhãn MÔ PHỎNG.
Hướng dẫn cấu hình hai máy: [test_two_machines.md](docs/test_two_machines.md).

Để dùng thiết bị thật:

```powershell
python -m control_station.main --hardware
```

Chương trình khởi động OFF, đọc cảm biến và đồng bộ hai tải OFF trước khi cho
chọn MANUAL/AUTO trên HMI. Ctrl+C hủy vòng chạy, thử xác nhận tải OFF rồi đóng bus.
Mất giao tiếp hoặc dữ liệu sai chốt FAULT; kết nối lại không tự tiếp tục AUTO.
HMI cục bộ chỉ reset khi nguyên nhân đã hết và relay xác nhận tải OFF.

Có thể chọn một file cấu hình khác cho cả hai chế độ bằng `--config đường_dẫn`.

## Cấu hình vận hành

`configuration.py` nạp và kiểm tra YAML trước khi mở COM. `ControlSettings`
không giữ bản mặc định riêng; HMI, runtime thật và mô phỏng nhận cùng thông số.
Đổi YAML rồi khởi động lại chương trình để áp dụng.

- Bus hiện tại: COM5, 9600 N81, timeout 1000 ms, 3 lần thử lại, chờ 100 ms.
- PT100 ID 2, relay ID 1. Đổi YAML không tự đổi ID của thiết bị.
- Setpoint 60 °C, deadband 2 °C, giới hạn quá nhiệt 90 °C, khoảng đặt 20–80 °C.
- `heater_to_fan_start_delay_s: 5.0`: trễ bật quạt sau khi bắt đầu gia nhiệt.
- `drying_duration_s: 600.0`: mặc định giữ nhiệt đủ điều kiện 10 phút; HMI nhập theo phút.
- `cooling_stop_temperature_c: 35.0`: làm nguội đến 35 °C mới tắt quạt.
- `fan_start_confirmation_timeout_s: 10.0`: hạn ghi + đọc xác nhận quạt mỗi lần thử.
- `runtime.poll_interval_ms`: chu kỳ đọc nhiệt độ và điều khiển.
- `hmi.host/port`: địa chỉ HMI.
- `simulation`: chỉ là thông số mô hình nhiệt minh họa, không dùng tính thời gian sấy.

### Gán relay và tiếp điểm

```yaml
relay:
  outputs:
    heater: {channel: relay_1, contact: "NO"}
    process_fan: {channel: relay_2, contact: "NO"}
```

Đổi channel theo dây thực tế; mỗi tải dùng một kênh. Chọn `"NC"` nếu đấu COM-NC.
Phải viết NO/NC trong dấu nháy vì YAML có thể diễn giải NO thành boolean.
Driver chuyển trạng thái tải ON/OFF sang trạng thái cuộn relay tương ứng.
Với tải NC, tắt tải cần cấp điện cuộn relay; mất nguồn module sẽ đóng NC trở lại.
Readback coil chỉ xác nhận trạng thái module, không chứng minh tải có điện hay quạt quay.

## Logic điều khiển

MANUAL cho bật/tắt gia nhiệt và quạt tuần hoàn độc lập, có thể cùng ON.
AUTO chạy một mẻ sấy theo năm pha:

| Pha      | Gia nhiệt                           | Quạt                               | Chuyển pha                                                                         |
| -------- | ------------------------------------ | ----------------------------------- | ----------------------------------------------------------------------------------- |
| IDLE     | OFF                                  | OFF                                 | Nhận bắt đầu mẻ; chu kỳ kế tiếp chạy HEATING hoặc HOLDING nếu đã nóng |
| HEATING  | ON khi chưa đạt điểm đặt      | Bật sau thời gian trễ cấu hình | T ≥ setpoint                                                                       |
| HOLDING  | Bật tại T ≤ S-D; tắt tại T ≥ S | ON liên tục                       | Đủ thời gian giữ nhiệt trong vùng S ± D                                      |
| COOLING  | OFF, không bật lại                | ON liên tục                       | T ≤ ngưỡng làm nguội                                                           |
| COMPLETE | OFF                                  | OFF                                 | Chờ người vận hành bắt đầu mẻ mới                                         |

S là setpoint, D là deadband. Thời gian sấy chỉ bắt đầu khi đạt S và quạt đã
được xác nhận ON. Trong HOLDING, bộ đếm tạm dừng khi T ngoài [S-D, S+D], giữ
thời gian đã tích lũy và tiếp tục khi nhiệt quay lại vùng. Controller dùng đồng hồ
monotonic, chỉ cộng khoảng giữa hai mẫu liên tiếp đều trong vùng; bỏ khoảng chuyển
tiếp ra/vào vùng, không suy đoán diễn biến giữa hai mẫu. Gia nhiệt và làm nguội
không tính vào thời gian sấy. Ví dụ chọn 10 phút thì cần đủ 600 giây giữ nhiệt đủ
điều kiện; tổng thời gian mẻ có thể dài hơn.

Trên HMI: lưu nhiệt độ, **thời gian sấy theo phút** và ngưỡng nguội, rồi bấm
**Bắt đầu sấy**. AUTO cũng bắt đầu một mẻ. HMI hiển thị pha, thời gian đủ điều
kiện đã tích lũy/còn lại, trạng thái đang đếm hoặc tạm dừng. Thông số bị khóa
trong HEATING/HOLDING/COOLING; lệnh bắt đầu lại bị từ chối để không reset bộ đếm
của mẻ đang chạy. Sau COMPLETE có thể chạy mẻ mới; chỉnh thông số sau COMPLETE
đưa về OFF sẵn sàng. Thay đổi HMI chỉ lưu trong RAM của phiên chạy, không ghi
YAML; khởi động lại lấy mặc định YAML. Muốn đổi mặc định, sửa YAML rồi khởi động lại.

Khi COOLING, HMI nhắc **mở cửa thoát ẩm thủ công**. Chưa có actuator/driver cửa,
không có tín hiệu chứng minh cửa đã mở. Ngưỡng nguội phải >= 0 và thấp hơn S-D;
nếu nhiệt độ môi trường cao hơn ngưỡng này, mẻ sẽ tiếp tục chờ làm nguội.
`fan_post_run_s` và các pha preheat/post-run cũ đã được thay thế; thời gian quạt
làm nguội hiện phụ thuộc nhiệt độ, không còn cố định 10 giây.

Bật quạt lỗi/quá hạn thì tắt gia nhiệt và thử lại đúng một lần. Retry xác nhận
được quạt: heater giữ OFF trong chu kỳ đó; chỉ có thể bật lại ở chu kỳ an toàn kế
tiếp nếu còn HEATING. Cả hai lần đều lỗi thì chốt PROCESS_FAN_START_FAILED và
hủy mẻ. Thời hạn xác nhận quạt chỉ được kiểm tra sau khi lời gọi Modbus đồng bộ
trả về, không ngắt giao dịch đang chờ. Coil readback không chứng minh quạt quay.

Lỗi cảm biến/quá nhiệt yêu cầu gia nhiệt OFF, quạt ON nếu bus còn điều khiển được.
Dừng khẩn, lỗi relay và các lỗi khác yêu cầu hai tải OFF. OFF, chuyển MANUAL,
FAULT, shutdown hoặc restart hủy mẻ và xóa bộ đếm; không tự tiếp tục mẻ cũ.
Mất MQTT không làm dừng điều khiển cục bộ.
Thời gian/nhiệt độ là thông số thí nghiệm người vận hành chọn, chưa có mô hình
độ ẩm giấy hoặc cảm biến ẩm để chứng minh giấy đã khô.

## Các khối và API

- `modbus.py`: sở hữu cổng serial và RLock tuần tự hóa giao dịch.
- `PT100Sensor.read_sample()`: trả (raw, °C); `read_temperature()`: trả mẫu °C mới.
  Phản hồi sai/ngoài dải cấu hình gây PT100DataError; giao tiếp lỗi gây
  ModbusCommunicationError. Không dùng lại mẫu cũ khi đọc lỗi.
- `RelayModule`: set_heater, set_process_fan, read_states, all_outputs_off.
  Giá trị boolean của API là trạng thái tải, đã xét tiếp điểm NO/NC.
- `SystemController`: quyết định đầu ra và quản lý OFF/MANUAL/AUTO/FAULT.
- `StationRuntime`: đọc PT100 rồi chạy controller bằng một bus chung.
- HMI: GET /api/state, POST /api/commands, GET /api/health; frontend polling 1 giây.
  Tên trạng thái/lệnh thống nhất là process_fan_on / SET_PROCESS_FAN.
  HMI gọi public API controller trực tiếp; RPC MQTT được đưa vào hàng đợi và xử lý
  trong vòng điều khiển, không gọi relay/Modbus từ callback cloud.

## Script thử thiết bị

```powershell
python -m control_station.tests.read_pt100
python -m control_station.tests.temperature_relay_sequence --drying-minutes 10 --max-wait-seconds 1800
python -m control_station.tests.nc_temperature_cutoff
python -X utf8 -m control_station.tests.relay_sequence --set-id 1
python -X utf8 -m control_station.tests.relay_sequence --scan-id-only
python -X utf8 -m control_station.tests.relay_sequence --slave-id 1 --channel 1 --check-id-only
python -m control_station.tests.relay_sequence --help
```

- Chạy `python -X utf8 -m control_station.tests.relay_sequence` không kèm tham
  số để vào menu tương tác: `doc` dò/đọc ID, `docrelay1-2` chỉ đọc trạng thái,
  `battat` chạy chuỗi bật/tắt cả hai relay, `thoat` kết thúc.
- read_pt100 đọc ID cấu hình; Windows có phím T bật test, S bắt đầu/dừng đồng hồ.
  Đổi ID tường minh: `--change-id --current-id ID_hiện_tại`.
  `--scan` chỉ dò khi đã cô lập PT100 khỏi các module khác.
- temperature_relay_sequence chạy một mẻ bằng cùng AUTO/runtime của HMI đến COMPLETE;
  `--drying-minutes` thay thời gian sấy, `--max-wait-seconds` là hạn chờ toàn bộ mẻ.
  Hết hạn chờ khi chưa COMPLETE báo thất bại, hủy mẻ và thử tắt tải.
  Mặc định các thông số mẻ lấy từ YAML, không phải một logic riêng.
- nc_temperature_cutoff là phép thử riêng cho hai tải COM-NC: chạy đến ngưỡng
  (mặc định setpoint YAML), rồi ngắt cả hai. Đây không phải chu trình AUTO.
- relay_sequence chẩn đoán cuộn relay, gồm thao tác commissioning theo CLI;
  `--set-id 1` ghi ID bằng FC16 broadcast và xác nhận thanh ghi ID qua FC03;
  `--scan-id-only` chỉ đọc thanh ghi ID bằng FC03 broadcast ở các baud đã cấu
  hình, không bật/tắt relay. Chỉ dò khi relay được cấp nguồn và cô lập khỏi
  PT100/các slave khác trên bus để tránh nhiều thiết bị cùng phản hồi broadcast.
  `--channel 1` hoặc `--channel 2` chỉ đọc/test riêng một coil, hữu ích khi
  kiểm tra trạng thái từng kênh. Các yêu cầu FC01 đều đọc từ coil 0 với số
  lượng 8 theo datasheet; script lấy bit 0/1 tương ứng Relay 1/2. Kèm
  `--check-id-only` để chỉ đọc, hoặc bỏ tùy chọn đó để chạy chuỗi ON/OFF cho
  riêng kênh đã chọn.
  Sau khi xác nhận baud và ID, cập nhật `modbus.baudrate` và `relay.slave_id`
  trong YAML. Chạy không có `--scan-id-only` sẽ bật/tắt lần lượt hai coil thật;
  tháo tải nguy hiểm/cách ly cơ cấu chấp hành trước khi thử. Bài test chẩn đoán
  cuộn relay, không đại diện trạng thái tải khi đấu NC.

## Kiểm thử

```powershell
python -X utf8 -m unittest discover -s control_station/tests -q
```

Bộ test dùng bus/relay/đồng hồ giả và Flask test client, không mở thiết bị thật.

## ThingsBoard và SCADA từ xa

SCADA có bốn màn Vận hành, Xu hướng, Cảnh báo và Chẩn đoán. Xem
[hướng dẫn giao diện SCADA](docs/scada_ui.md) để biết các trạng thái dữ liệu,
luồng áp dụng thông số và cách chạy bản xem thử cục bộ không cần phần cứng:

```powershell
python -m remote_scada.preview
```

Mở `http://127.0.0.1:8081`, đăng nhập `operator` / `demo-scada`. Đây là bản
mô phỏng localhost với lịch sử nền minh họa, không kết nối ThingsBoard.
Khi chạy SCADA qua ThingsBoard, cập nhật cả hai máy để hỗ trợ `SET_RECIPE`.

Máy 1 vẫn là bộ điều khiển duy nhất cho PT100/Modbus/relay. Cầu nối MQTT tùy chọn
gửi telemetry mỗi giây và nhận RPC; callback chỉ xếp lệnh vào hàng đợi, sau đó
`StationRuntime` chuyển lệnh cho `SystemController`. Mất kết nối cloud không dừng
vòng điều khiển cục bộ. Để bật MQTT trên máy 1:

```powershell
Copy-Item control_station\.env.example control_station\.env
python -m pip install -r control_station\requirements.txt
```

Điền `TB_DEVICE_TOKEN` của đúng thiết bị máy 1 trong `control_station\.env`, rồi
chạy `python -m control_station.main --hardware` cho phần cứng hoặc
`python -m control_station.main --mqtt` cho mô phỏng. Không đưa Device Token này
lên máy chạy SCADA.

Để kiểm tra riêng kết nối ThingsBoard mà không khởi động phần cứng, có thể chạy
`python control_station\tests\thingsboard_telemetry_smoke.py` từ thư mục gốc.
Script đọc cấu hình MQTT trong `control_station\.env`, yêu cầu xác nhận trước
khi gửi dữ liệu giả mỗi 2 giây, và dừng bằng Ctrl+C. Dữ liệu giả ghi đè các
telemetry mới nhất của cùng thiết bị; không chạy đồng thời với controller thật
đang gửi lên thiết bị đó. Port 1883 không mã hóa; ưu tiên TLS trên port 8883.

SCADA trên máy 2 có backend Flask riêng, đăng nhập tài khoản SCADA trước khi xem
telemetry; backend dùng credential ThingsBoard để lấy telemetry/lịch sử qua REST,
nhận cập nhật trực tiếp qua WebSocket và gửi two-way RPC. Frontend chỉ gọi backend
SCADA. Cấu hình và chạy từ thư mục gốc repository:

```powershell
Copy-Item remote_scada\.env.example remote_scada\.env
python -m pip install -r remote_scada\requirements.txt
python -m remote_scada.server
```

Chỉ chạy lệnh `Copy-Item` khi chưa có `remote_scada\.env`; lệnh này tạo cấu hình
mới từ mẫu, không cần chạy lại hoặc ghi đè file `.env` đã được điền.
Điền URL, username/password ThingsBoard, UUID thiết bị, tài khoản SCADA và secret
key riêng trong `remote_scada\.env`. Chạy mặc định tại `http://127.0.0.1:8080`.
Sau khi lệnh chạy, trên chính máy 2 mở Chrome/Edge và nhập
`http://127.0.0.1:8080` vào thanh địa chỉ (không cần ThingsBoard tạo link hay vào
dashboard ThingsBoard). Đăng nhập bằng `SCADA_USERNAME` và `SCADA_PASSWORD` trong
`remote_scada\.env`; sau đó dùng nút **Bắt đầu sấy**, **Chuyển OFF** hoặc các
thông số để gửi yêu cầu đến controller máy 1. Lệnh chỉ tới được máy 1 nếu runtime
máy 1 đang chạy `--hardware` hoặc mô phỏng với `--mqtt`, đã kết nối MQTT ThingsBoard và backend máy 2 dùng
đúng thiết bị/credential ThingsBoard.
Để truy cập qua mạng, cấu hình `SCADA_HOST=0.0.0.0`, đặt sau HTTPS reverse proxy
được tin cậy và bật `SCADA_COOKIE_SECURE=true`; không chạy Flask development server
trực tiếp như một dịch vụ Internet. Không dùng chung tài khoản SCADA và credential
ThingsBoard, không commit `.env`. SCADA gắn lịch sử alarm dưới telemetry key
`alarm_event`, theo retention đã cấu hình trên ThingsBoard.

Máy 2 chỉ được gửi `SET_MODE` AUTO/OFF, `START_DRYING`, thay đổi thông số khi mẻ
chưa chạy và `EMERGENCY_STOP` (có xác nhận ở giao diện); không điều khiển relay
MANUAL hoặc reset cảnh báo. Hợp đồng dữ liệu nằm trong
[communication_contract.md](docs/communication_contract.md).
