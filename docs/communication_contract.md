# Hợp đồng dữ liệu máy 1

Phiên bản schema cục bộ: **3 — mẻ sấy theo thời gian giữ nhiệt**.
Tên quạt là process_fan. Các pha AUTO cũ và fan_post_run_s đã được thay thế.

## GET /api/state

Trả object gồm state, settings, relay_feedback, active_alarm, simulation và server_time_ms.

state gồm:

- temperature_c: số °C hoặc null khi không có mẫu hợp lệ.
- heater_on, process_fan_on: bool, trạng thái tải cuối xác nhận qua coil.
- auto_phase: IDLE / HEATING / HOLDING / COOLING / COMPLETE.
- drying_elapsed_s: thời gian giữ nhiệt đủ điều kiện đã tích lũy, đơn vị giây.
- drying_remaining_s: thời gian đủ điều kiện còn cần; không phải dự báo thời gian kết thúc mẻ.
- hold_timer_running: true chỉ khi HOLDING và nhiệt độ trong [setpoint-deadband, setpoint+deadband].
- mode: OFF / MANUAL / AUTO / FAULT.
- alarm_code: mã lỗi hiện tại.
- sensor_connected, modbus_connected, mqtt_connected: bool.
- relay_connected: bool hoặc null; HMI bổ sung từ phản hồi driver relay, độc lập với trạng thái bus.
  null là chưa kiểm tra, false là mất bus hoặc lỗi phản hồi module, true là phản hồi hợp lệ gần nhất.
- timestamp_ms: thời điểm controller cập nhật, Unix milliseconds.

relay_feedback gồm heater và process_fan, mỗi giá trị bool hoặc null. Đây là trạng thái
**tải** từ cache readback của RelayModule, đã xét NO/NC, không lấy trạng thái bus để
suy ra module relay có phản hồi. Không xác nhận kết nối thì hai giá trị là null.
HMI hardware nhận driver qua create_app(..., relay=runtime.relay); demo dùng relay
trong RAM và có nhãn MÔ PHỎNG. GET không gửi giao dịch Modbus.

Runtime kiểm tra read_states mỗi giây trên bus chung. Với hai coil liên tiếp,
chỉ dùng một FC01 đọc cả hai. Lỗi probe cập nhật trạng thái hiển thị, không tự đổi
chính sách FAULT của controller. Giao dịch vẫn dùng timeout/retry hiện có, nên
kiểm tra có thể kéo dài hơn một giây khi thiết bị không phản hồi.

Sơ đồ SVG lấy relay_feedback đã xác nhận để quay quạt, chạy mũi tên tuần hoàn,
hiện hiệu ứng gia nhiệt và trạng thái hai kênh tải. Không đổi hiệu ứng ngay theo
nút bấm; lệnh bị từ chối không giả định tải đổi trạng thái. Mất phản hồi relay
hoặc lỗi GET/timeout HMI thì dừng hiệu ứng và hiển thị chưa xác nhận. Coil readback
không chứng minh quạt thật đang quay; cửa thoát ẩm chỉ là hình tĩnh/mở thủ công.

settings gồm temperature_setpoint_c, control_deadband_c, high_temperature_limit_c,
minimum_setpoint_c, maximum_setpoint_c, heater_to_fan_start_delay_s,
drying_duration_s, cooling_stop_temperature_c, fan_start_confirmation_timeout_s.
YAML cung cấp mặc định khi khởi động. HMI thay đổi ba thông số mẻ trong RAM của
phiên chạy; không ghi ngược vào YAML. Ngưỡng nguội >= 0 và < setpoint-deadband.
Vùng giữ nhiệt phải dưới giới hạn quá nhiệt; thời gian sấy hữu hạn và > 0.

active_alarm là null hoặc Alarm với code, severity, message, alarm_id, active,
raised_at_ms, cleared_at_ms. simulation phân biệt mô phỏng với thiết bị thật.
Readback coil chưa phải phản hồi vật lý. Khi mất bus giữ trạng thái tải cuối
đã xác nhận; không kết luận tải đã OFF.

## POST /api/commands

SCADA mới gửi một lệnh `SET_RECIPE` với `value` là object gồm đúng
`temperature_setpoint_c`, `drying_duration_s`, `cooling_stop_temperature_c`.
Controller kiểm tra toàn bộ và áp dụng nguyên tử; lỗi giữ nguyên cả bộ.
Lệnh không được dùng khi mẻ đang chạy, không tác động relay và không thay
quyền reset FAULT. Các lệnh thay từng thông số vẫn tương thích như trước.
MQTT thêm bốn khóa giới hạn `minimum_setpoint_c`, `maximum_setpoint_c`,
`control_deadband_c`, `high_temperature_limit_c` để giao diện lấy giới hạn
từ controller. Cần cập nhật code ở cả hai máy; không có fallback gửi từng
giá trị khi máy 1 chưa hỗ trợ `SET_RECIPE`.

Ví dụ đặt 10 phút: {"command_type":"SET_DRYING_DURATION","value":600}.
HMI nhập phút và đổi sang giây trước khi gửi; API luôn dùng giây.

| Lệnh | value | Điều kiện |
|---|---|---|
| SET_TEMPERATURE | °C, số hữu hạn | Setpoint hợp lệ; mẻ chưa chạy |
| SET_DRYING_DURATION | Giây, số hữu hạn > 0 | Mẻ chưa chạy |
| SET_COOLING_TEMPERATURE | °C, số hữu hạn | Ngưỡng nguội hợp lệ; mẻ chưa chạy |
| START_DRYING | null hoặc bỏ value | Khởi động mẻ mới; không FAULT hoặc đang chạy |
| SET_MODE | OFF / MANUAL / AUTO | AUTO cũng bắt đầu mẻ; không reset mẻ đang chạy |
| SET_HEATER / SET_PROCESS_FAN | bool | Chỉ MANUAL |
| RESET_ALARM | null | Chỉ LOCAL_HMI, dữ liệu an toàn và relay xác nhận OFF |
| EMERGENCY_STOP | Bỏ qua payload | Khóa FAULT, thử tắt cả hai tải |

Mẻ đang chạy gồm AUTO/IDLE, HEATING, HOLDING, COOLING. COMPLETE chốt hoàn tất;
nhiệt giảm không bắt đầu lại. Chỉnh thông số ở COMPLETE đưa về OFF và xóa tiến độ
cũ. OFF/MANUAL, FAULT, shutdown và restart hủy mẻ, xóa bộ đếm; reset chỉ đưa về OFF.

Bộ đếm bắt đầu khi đã đạt điểm đặt và quạt ON. Chỉ cộng khoảng giữa hai mẫu liên
tiếp đều trong vùng; ra/vào vùng không cộng khoảng chuyển tiếp. Ngoài vùng tạm
dừng, không xóa tiến độ. Đủ thời gian -> COOLING; gia nhiệt không bật lại; đến
ngưỡng nguội -> hai tải OFF/COMPLETE. HMI nhắc mở cửa thủ công, chưa điều khiển cửa.

HMI gán LOCAL_HMI, không nhận source từ body. Controller dùng RLock. Kết quả gồm
command_id, accepted, message, timestamp_ms và state. HTTP 200 khi chấp nhận,
409 khi controller từ chối, 400 khi body/tên lệnh sai. Bool không được dùng làm
số thông số. Lệnh cấu hình và bắt đầu không tác động relay trước chu kỳ tiếp theo.

## Telemetry và quyền từ xa

Máy 1 tiếp tục là bộ điều khiển duy nhất cho phần cứng. Máy 2 là trạm giám sát và
gửi yêu cầu cấp cao; không truy cập Modbus/relay. Hai máy trao đổi qua ThingsBoard:
máy 1 dùng MQTT/TLS với Device Token để gửi telemetry và nhận RPC; backend SCADA
máy 2 dùng REST/WebSocket với JWT để đọc telemetry/lịch sử và gửi RPC. Frontend
SCADA chỉ gọi backend máy 2, không giữ credential ThingsBoard hoặc Device Token.

Toàn bộ SCADA yêu cầu đăng nhập trước khi cho xem dữ liệu hoặc gửi lệnh. Mật khẩu
tài khoản SCADA không được gửi xuống frontend dưới dạng cấu hình/secret; credential
ThingsBoard của backend và Device Token máy 1 là hai loại thông tin riêng biệt.

TelemetryRecord là cơ sở cho telemetry; tên trường, kiểu và đơn vị dùng theo schema
ở trên. Máy 1 gửi telemetry định kỳ mỗi **1 giây**; đây là nhịp gửi cloud, không
thay đổi chu kỳ điều khiển cục bộ (`runtime.poll_interval_ms`). Máy 2 hiển thị
trạng thái hiện tại, tiến độ mẻ, kết nối và biểu đồ lịch sử telemetry.

Telemetry có thêm `simulation: bool`: true khi máy 1 chạy mô phỏng với `--mqtt`,
false khi dùng `--hardware`. SCADA dùng trường này để gắn nhãn MÔ PHỎNG.
Mô phỏng dùng cùng controller, hàng đợi RPC và quyền lệnh từ xa như phần cứng,
nhưng nhiệt độ được tính bằng mô hình nhiệt và relay chỉ nằm trong RAM; không mở COM.

Alarm hiện tại tiếp tục được cung cấp trong trạng thái telemetry. Ngoài ra máy 1
phải phát sự kiện khi alarm được kích hoạt và khi được xóa, với `alarm_id`, `code`,
`severity`, `message` và timestamp tương ứng (`raised_at_ms`/`cleared_at_ms`).
ThingsBoard lưu sự kiện theo chính sách retention đang cấu hình trên nền tảng;
SCADA hiển thị lịch sử alarm từ các sự kiện này. Máy 2 không tự suy ra lịch sử chỉ
từ trạng thái alarm hiện tại.

SCADA nhận kết quả accepted/rejected do controller trả về, kèm mã lệnh và thông
điệp. Accepted chỉ có nghĩa controller chấp nhận/xử lý yêu cầu; không chứng minh
tải vật lý đã hoạt động. Trạng thái đầu ra được hiển thị riêng theo telemetry và
relay readback; coil readback cũng không chứng minh tải thật hoặc quạt đang chạy.

Các thông số mẻ thay đổi từ xa chỉ được giữ trong RAM của máy 1, không ghi ngược
YAML và không lưu bền vững. Sau restart, máy 1 trở về settings trong YAML, khởi động
OFF, hủy tiến độ mẻ và không tự chạy lại. Sau khi kết nối lại, SCADA phải lấy lại
settings/state từ máy 1 thay vì coi giá trị cũ ở giao diện là đang áp dụng.

REMOTE_SCADA chỉ được gửi `SET_MODE` với AUTO/OFF, `START_DRYING`,
`SET_TEMPERATURE`, `SET_DRYING_DURATION`, `SET_COOLING_TEMPERATURE` khi mẻ chưa
chạy, và `EMERGENCY_STOP`. Giao diện yêu cầu xác nhận trước khi gửi dừng khẩn.
Không cho phép điều khiển tải MANUAL (`SET_HEATER`/`SET_PROCESS_FAN`) hoặc
`RESET_ALARM` từ xa. Máy 1 kiểm tra mọi lệnh; lệnh thường quá 5 giây bị từ chối.
Mất cloud không dừng điều khiển cục bộ.

Máy 1 đã có cầu nối MQTT/TLS tùy chọn để gửi telemetry mỗi giây, nhận two-way RPC,
đưa lệnh vào vòng điều khiển và phản hồi accepted/rejected. `active_alarm` và
`alarm_event` được gửi dạng JSON string; `alarm_event` là một sự kiện hoặc danh sách
sự kiện raised/cleared để giữ cả hai chuyển trạng thái trong cùng một chu kỳ publish.
Máy 2 có backend Flask đăng nhập, dùng REST với JWT cho telemetry/lịch sử/RPC và
WebSocket ThingsBoard để nhận telemetry mới; frontend chỉ gọi API backend.
Lịch sử alarm nằm trong timeseries key `alarm_event` và tuân theo retention
ThingsBoard, không có database cục bộ riêng.

Việc trao đổi thực tế vẫn cần khai báo đúng ThingsBoard URL, device UUID, quyền
credential backend, Device Token máy 1 và kết nối TLS trên hai máy; test tự động
không thay cho commissioning với ThingsBoard/thiết bị thật.

Thời gian xử lý I/O được tính trong chu kỳ điều khiển cục bộ. Giám sát relay
định kỳ chỉ thử một lần mỗi kỳ; ghi relay và đọc xác nhận sau ghi vẫn dùng
chính sách retry trong YAML. SCADA tải lịch sử độc lập với cập nhật trạng thái,
và tách phiên HTTP cho telemetry, lịch sử và RPC. Xem [thời gian phản hồi và
giới hạn](timing.md) để biết ngân sách chờ và cách kiểm tra.
