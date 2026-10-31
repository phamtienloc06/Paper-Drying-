# Nhật ký phát triển

Các mục dưới đây được ghi bù từ kết quả triển khai và kiểm tra trong phiên làm việc.
Thời gian ghi theo UTC+7. Thời điểm ghi bù không phải thời điểm hoàn thành chính xác;
giờ hoàn thành từng mốc chưa được ghi nhận. Nhật ký không chứa thông tin xác thực
hoặc dữ liệu bí mật.

## 1. Triển khai mô hình dữ liệu máy 1

- **Thời gian UTC+7:** 2026-09-26 19:56:31 (ghi bù).
- **Mục tiêu:** Xây dựng các kiểu dữ liệu dùng chung cho chương trình điều khiển
  máy 1 bằng Python 3.10+, chỉ sử dụng thư viện chuẩn.
- **File thay đổi:** `control_station/models.py`.
- **Nội dung chính:**
  - Triển khai 5 Enum kế thừa `str, Enum`: `ControlMode`, `CommandType`,
    `CommandSource`, `AlarmCode`, `AlarmSeverity`.
  - `AlarmCode.EMERGENCY_STOPPED` được bổ sung sau phiên triển khai ban đầu;
    đã xác nhận có trong file trước khi triển khai controller. Đây là thành viên
    bổ sung của `AlarmCode`, không phải Enum thứ sáu.
  - Triển khai 6 dataclass: `SystemState`, `ControlSettings`, `ControlCommand`,
    `CommandResult`, `Alarm`, `TelemetryRecord`.
  - Bổ sung `CommandValue`, helper `_now_ms()`, UUID4 tự sinh cho lệnh/cảnh báo
    và timestamp Unix milliseconds qua `default_factory`.
  - Có `from __future__ import annotations` và docstring tiếng Việt.
- **Kiểm tra:** Đều đạt trong phiên triển khai.
  - Import đầy đủ các Enum, dataclass và type alias.
  - Tạo đủ 6 object mẫu, gồm lệnh đặt nhiệt độ 65°C từ `LOCAL_HMI`.
  - Kiểm tra giá trị mặc định, UUID4 tự sinh, timestamp và chuyển JSON bằng
    `json.dumps(asdict(...))`.
  - Kiểm tra cú pháp Python 3.10 và biên dịch bằng entry point `py_compile`
    qua `runpy`; thao tác ghi bytecode được chặn để không tạo file `.pyc`.
  - Script kiểm tra truyền qua stdin với `python -B -X utf8 -`, không lưu file test.
- **Ghi chú/phần chưa triển khai:** Dataclass chỉ chứa dữ liệu, không có validation
  hoặc logic điều khiển. Kiểm tra giới hạn thuộc controller. Kết quả kiểm tra
  ban đầu có trước lần bổ sung `EMERGENCY_STOPPED`; thành viên này được sử dụng
  và kiểm tra trong mốc controller bên dưới.

## 2. Triển khai bộ điều khiển máy 1

- **Thời gian UTC+7:** 2026-09-26 19:56:31 (ghi bù).
- **Mục tiêu:** Xây dựng bộ điều khiển có thể kiểm thử độc lập với phần cứng,
  quản lý chế độ, xử lý lệnh và áp dụng kiểm tra an toàn.
- **File thay đổi:** `control_station/controller.py`; không sửa `models.py`
  trong mốc này.
- **Nội dung chính:**
  - Triển khai Protocol `RelayPort` và class `SystemController`.
  - Cung cấp API cập nhật phép đo/kết nối MQTT, chạy chu kỳ điều khiển, xử lý
    lệnh, lấy snapshot trạng thái/thông số và lấy cảnh báo đang hoạt động.
  - Quản lý OFF/MANUAL/AUTO/FAULT; AUTO dùng hysteresis, có liên động heater–quạt.
  - Kiểm tra mất Modbus, lỗi cảm biến, quá nhiệt và lỗi gọi relay; mất MQTT
    không làm dừng điều khiển cục bộ.
  - Phân quyền HMI/SCADA/SYSTEM, từ chối lệnh remote quá 5 giây; dừng khẩn
    không bị bỏ qua vì timestamp. FAULT chỉ được reset hợp lệ từ LOCAL_HMI.
  - Dùng `RLock` và deep copy; đồng bộ đầu ra OFF khi kết nối lần đầu/kết nối lại.
    Chỉ cập nhật trạng thái đầu ra sau lời gọi relay thành công và tránh ghi lặp.
  - Bắt lỗi relay để không làm chết vòng điều khiển; không giả định tải thật
    đã tắt khi mất phản hồi Modbus.
- **Kiểm tra:** 19 nhóm tình huống yêu cầu với `FakeRelay` đều đạt.
  - Khởi động OFF, đổi setpoint trong OFF/MANUAL, điều khiển thủ công và thứ tự
    tắt đầu ra đối diện trước khi bật heater/quạt.
  - Chuỗi hysteresis với setpoint 30°C, deadband 2°C tại
    20 → 29 → 30 → 31 → 32 → 31 → 30°C.
  - Phân quyền SCADA, hết hạn lệnh và dừng khẩn từ mọi nguồn.
  - Mất cảm biến/Modbus/MQTT, quá nhiệt, điều kiện từ chối reset và reset về OFF.
  - Exception relay, snapshot deep copy và bất biến heater/quạt không cùng bật.
  - Kiểm tra bổ sung dữ liệu lệnh sai, số không hữu hạn, kết nối lại, tránh ghi
    lặp và dừng khẩn với timestamp không hợp lệ.
  - Import và cú pháp Python 3.10 đạt. Entry point `py_compile` chạy qua `runpy`,
    chặn ghi `.pyc`; script kiểm tra chạy qua stdin bằng `python -B -X utf8 -`.
- **Ghi chú/phần chưa triển khai:** Kiểm thử dùng relay giả, chưa xác minh trên
  thiết bị thật. Chưa triển khai driver relay/Modbus/PT100, tích hợp vòng lặp
  trong `main.py`, HMI, MQTT, SCADA hoặc database. Mốc này không cài thư viện,
  chạy server, lưu script test hay thay đổi Git.

## 3. Bổ sung bộ kiểm thử cố định cho chế độ OFF

- **Thời gian UTC+7:** 2026-09-27 08:57:29.
- **Mục tiêu:** Thêm kiểm thử chế độ OFF của `SystemController` bằng `unittest`
  trong thư viện chuẩn, sử dụng public API và relay giả.
- **File được tạo/sửa:**
  - Tạo `control_station/tests/test_controller.py`.
  - Nối mục hiện tại vào `docs/development_log.md`.
  - Không sửa `models.py`, `controller.py` hoặc tạo `tests/__init__.py`.
- **Nội dung chính:** Thêm `FakeRelay` có trạng thái đầu ra ban đầu tùy chọn,
  lịch sử lệnh và class `TestSystemControllerOffMode`. Mỗi test có dữ liệu riêng,
  docstring/comment tiếng Việt, không truy cập thuộc tính private của controller.
- **Các hành vi đã kiểm thử:**
  1. Khởi động mặc định OFF, hai đầu ra tắt và không có cảnh báo.
  2. Chu kỳ an toàn đầu tiên ở 25°C đồng bộ relay còn bật về OFF; kiểm tra
     hai trường hợp ban đầu bằng subtest: heater bật hoặc quạt bật.
  3. Chấp nhận setpoint 30°C từ LOCAL_HMI khi nhiệt độ 20°C mà không bật tải.
  4. Từ chối lệnh bật heater trực tiếp từ LOCAL_HMI trong OFF.
  5. Từ chối lệnh bật quạt trực tiếp từ LOCAL_HMI trong OFF.
  6. Tiếp tục cập nhật phép đo 25°C rồi 26,5°C và trạng thái kết nối trong OFF,
     trong khi hai đầu ra vẫn tắt.
- **Kiểm tra và kết quả:**
  - Chạy `python -B -m unittest discover -s control_station/tests -v`
    từ thư mục gốc dự án: **6 test, tất cả đạt (OK)**.
  - Kiểm tra biên dịch bằng entry point `py_compile` qua `runpy` với đối số
    `control_station/tests/test_controller.py`: **đạt**. Script truyền qua stdin
    bằng `python -B -X utf8 -`, chặn ghi bytecode để không tạo `.pyc`.
  - Chưa phát hiện sai khác của controller trong phạm vi các test OFF trên.
- **Ghi chú/phần chưa triển khai:** MANUAL, AUTO và FAULT chưa được thêm vào bộ
  test cố định. Không dùng phần cứng thật, sleep, COM, MQTT hoặc Flask; không
  cài thư viện, tạo script kiểm tra tạm, thay đổi Git hay chạy server.

## 4. Bổ sung kiểm thử cố định cho chế độ MANUAL

- **Thời gian UTC+7:** 2026-09-27 09:00:54.
- **Mục tiêu:** Bổ sung kiểm thử MANUAL bằng unittest và FakeRelay hiện có,
  giữ nguyên 6 test OFF.
- **Hai file được sửa:** `control_station/tests/test_controller.py` và
  `docs/development_log.md` (chỉ nối mục mới).
- **8 hành vi MANUAL đã kiểm tra:**
  1. LOCAL_HMI chuyển từ OFF an toàn sang MANUAL, đầu ra tắt, không có alarm.
  2. Bật/tắt heater thủ công, quạt vẫn tắt.
  3. Bật/tắt quạt thủ công, heater vẫn tắt.
  4. Tắt heater trước khi bật quạt.
  5. Tắt quạt trước khi bật heater.
  6. Lưu setpoint 30°C nhưng không tự bật sưởi ở 20°C; giữ sưởi bật thủ công
     khi nhiệt độ lên 35°C và vẫn trong giới hạn an toàn.
  7. Từ chối REMOTE_SCADA yêu cầu vào MANUAL từ OFF.
  8. Từ chối REMOTE_SCADA điều khiển trực tiếp đầu ra khi đang MANUAL.
- **Kiểm tra và kết quả:**
  - Chạy `python -B -X utf8 -m unittest discover -s control_station/tests -v`:
    **14 test đạt**, gồm **6 test OFF cũ** và **8 test MANUAL mới**.
  - Kiểm tra biên dịch qua entry point py_compile bằng runpy và script stdin:
    **đạt**, chặn ghi bytecode nên không tạo file .pyc.
  - Kiểm tra thứ tự liên động trên phần lịch sử lệnh của lần chuyển tải,
    dựng lại đầu ra sau từng lệnh để phát hiện trường hợp cùng bật.
  - Chưa phát hiện sai khác của controller trong phạm vi đã kiểm thử.
- **Ghi chú/phần chưa triển khai:** AUTO và FAULT chưa được thêm vào bộ test
  cố định. Không bổ sung các nhóm lỗi kết nối, quá nhiệt, relay exception,
  deep copy hoặc hết hạn lệnh remote ở bước này. Không sửa models.py/controller.py,
  không tạo file/thư mục, cài thư viện, thay đổi Git hoặc chạy server.

## 5. Bổ sung kiểm thử cố định cho AUTO và hysteresis

- **Thời gian UTC+7:** 2026-09-27 09:04:30.
- **Mục tiêu:** Kiểm thử AUTO/hysteresis qua public API với unittest và FakeRelay,
  giữ nguyên 6 test OFF và 8 test MANUAL.
- **File được sửa:** `control_station/tests/test_controller.py` và
  `docs/development_log.md` (chỉ nối mục mới).
- **8 hành vi AUTO đã kiểm tra:**
  1. LOCAL_HMI chuyển AUTO an toàn, chưa bật tải trước control cycle tiếp theo.
  2. REMOTE_SCADA chuyển AUTO rồi trở về OFF, cả hai đầu ra tắt.
  3. Hysteresis sưởi: 20 → 29 → 30 → 29 → 28°C cho kết quả ON → ON → OFF → OFF → ON.
  4. Hysteresis quạt: 31 → 32 → 31 → 30°C cho kết quả OFF → ON → ON → OFF.
  5. Vùng trung tính 29/30/31°C giữ hai đầu ra OFF với setpoint 30°C, deadband 2°C.
  6. AUTO từ chối lệnh HMI điều khiển heater/quạt trực tiếp.
  7. Setpoint mới 24°C được áp dụng ở chu kỳ sau: tắt sưởi tại 25°C,
     chưa bật quạt; quạt bật khi cập nhật 26°C.
  8. Chuyển OFF tắt đầu ra đang hoạt động trong cả hai trường hợp sưởi và quạt,
     mỗi trường hợp dùng controller mới.
- **Kiểm tra và kết quả:**
  - Chạy `python -B -X utf8 -m unittest discover -s control_station/tests -v`:
    **22/22 test đạt**, gồm **14 test cũ vẫn đạt** và **8 test AUTO mới**.
  - Biên dịch file test bằng entry point py_compile qua runpy và stdin: **đạt**,
    chặn ghi bytecode để không để lại .pyc.
  - Kiểm tra snapshot, đầu ra relay giả và dựng lại lịch sử lệnh để xác nhận
    heater/quạt không đồng thời bật trong các tình huống AUTO đã kiểm thử.
  - Chưa phát hiện sai khác của controller trong phạm vi kiểm thử này.
- **Ghi chú/phần chưa triển khai:** FAULT và các lỗi an toàn chưa được thêm vào
  bộ test cố định. Không bổ sung các test mất kết nối, quá nhiệt, Emergency Stop,
  remote hết hạn, relay exception, deep-copy snapshot hoặc command sai kiểu.
  Không sửa models.py/controller.py, không tạo file mới, cài thư viện,
  thay đổi Git hoặc chạy server.

## 6. Bổ sung kiểm thử cố định cho FAULT và điều kiện an toàn

- **Thời gian UTC+7:** 2026-09-27 09:08:19.
- **Mục tiêu:** Kiểm thử FAULT, hành động an toàn, giữ lỗi và reset qua public API.
- **File được sửa:** `control_station/tests/test_controller.py` và
  `docs/development_log.md` (chỉ nối mục mới).
- **11 hành vi đã kiểm tra:**
  1. Dừng khẩn từ LOCAL_HMI ngắt sưởi đang chạy, tắt hai đầu ra và tạo FAULT.
  2. Dừng khẩn REMOTE_SCADA cũ 10 giây vẫn được xử lý.
  3. Mất cảm biến khi đang sưởi: tắt heater, bật quạt khi Modbus còn hoạt động.
  4. None, NaN và hai giá trị vô cực tạo SENSOR_DISCONNECTED; mỗi subtest có controller mới.
  5. Mất Modbus khi đang hoạt động tạo FAULT, không ném exception; không khẳng định
     tải vật lý đã tắt khi không còn giao tiếp.
  6. Nhiệt độ bằng và cao hơn giới hạn đều tạo OVER_TEMPERATURE, tắt sưởi và bật quạt.
  7. Mất MQTT không tạo FAULT; AUTO cục bộ vẫn bật/tắt sưởi theo phép đo.
  8. FAULT quá nhiệt vẫn được giữ khi nhiệt độ đã trở lại an toàn.
  9. REMOTE_SCADA không được reset dù điều kiện vật lý đã an toàn.
  10. LOCAL_HMI bị từ chối reset khi cảm biến/bus còn lỗi hoặc nhiệt độ còn
      cao hơn ngưỡng reset; mỗi tình huống dùng controller mới.
  11. LOCAL_HMI reset tại đúng ngưỡng an toàn thành công: về OFF, tắt tải,
      alarm_code NONE và không còn active alarm.
- **Kiểm tra và kết quả:**
  - Chạy `python -B -X utf8 -m unittest discover -s control_station/tests -v`:
    **33/33 test đạt**, gồm **22 test cũ vẫn đạt** và **11 test FAULT mới**.
  - Biên dịch file test qua entry point py_compile bằng runpy và stdin: **đạt**,
    chặn ghi bytecode nên không tạo .pyc hoặc script tạm.
  - Chưa phát hiện sai khác của controller trong các tình huống kiểm thử này.
- **Ghi chú/phần chưa triển khai:** Chưa bổ sung kiểm thử độ bền cho relay cố tình
  phát sinh exception, snapshot deep copy, lệnh remote thông thường hết hạn,
  command sai kiểu, setpoint ngoài giới hạn hoặc lệnh đồng thời/thread safety.
  Giữ nguyên 22 test cũ và lịch sử log; không sửa models.py/controller.py,
  không tạo file mới, cài thư viện, thay đổi Git hoặc chạy server.

## 7. Hoàn thành nhóm kiểm thử độ bền controller hiện tại

- **Thời gian UTC+7:** 2026-09-27 09:11:47.
- **Mục tiêu:** Kiểm thử độ bền trước lệnh không hợp lệ, lỗi relay và việc sửa snapshot bên ngoài.
- **File được sửa:** `control_station/tests/test_controller.py` và
  `docs/development_log.md` (chỉ nối mục mới).
- **Nội dung chính:** Mở rộng FakeRelay với fail_operations cho set_heater,
  set_cooling_fan và all_outputs_off. Lưu cả yêu cầu thất bại nhưng không cập nhật
  đầu ra khi phát sinh RuntimeError; mặc định giữ hành vi cũ.
- **9 hành vi đã kiểm tra:**
  1. Lệnh remote thông thường quá hạn bị từ chối, không đổi setpoint hoặc tác động đầu ra.
  2. Setpoint bool, chuỗi, None, NaN và vô cực bị từ chối mà không gây FAULT.
  3. Setpoint ngoài giới hạn bị từ chối; giá trị đúng bằng minimum/maximum được chấp nhận.
  4. Lệnh đầu ra MANUAL chỉ nhận bool, từ chối số 0/1, chuỗi và None.
  5. Mode không hợp lệ và SET_MODE yêu cầu FAULT bị từ chối, không đổi mode.
  6. Exception khi ghi heater được chặn, lệnh thất bại và tạo HEATER_CONTROL_FAILED.
  7. Exception khi đồng bộ OFF sau kết nối lại tạo FAULT, không xác nhận sai đầu ra
     đã tắt và vẫn thử đồng bộ ở chu kỳ sau.
  8. Sửa snapshot state/settings không thay đổi dữ liệu nội bộ.
  9. Sửa snapshot active alarm không thay đổi cảnh báo nội bộ.
- **Kiểm tra và kết quả:**
  - Chạy `python -B -X utf8 -m unittest discover -s control_station/tests -v`:
    **42/42 test đạt**, gồm **33 test cũ vẫn đạt** và **9 test độ bền mới**.
  - Kiểm tra biên dịch file test qua entry point py_compile bằng runpy/stdin:
    **đạt**, chặn ghi bytecode nên không tạo .pyc.
  - Chưa phát hiện sai khác controller trong phạm vi bộ kiểm thử.
- **Ghi chú/phần chưa triển khai:** Controller đã hoàn thành bộ unit test hiện tại
  theo phạm vi yêu cầu. Chưa kiểm thử tích hợp Modbus/relay thật hoặc thread safety.
  Không sửa models.py/controller.py, không tạo file mới, cài thư viện,
  thay đổi Git hoặc chạy server. Giữ nguyên 33 test và lịch sử log cũ.

## 8. Bổ sung giao tiếp Modbus RTU và script kiểm tra thiết bị EPCB

- **Thời gian UTC+7:** 2026-10-03 (ghi bù; giờ hoàn thành không được ghi nhận).
- **Mục tiêu:** Thêm tầng giao tiếp Modbus RTU dùng chung cho PT100 và relay EPCB,
  lưu register map theo datasheet và tạo script thử thiết bị chạy thủ công.
- **File được tạo/sửa:**
  - Sửa `control_station/modbus.py`.
  - Sửa `control_station/config.yaml`.
  - Sửa `control_station/requirements.txt`.
  - Tạo `control_station/tests/test_modbus.py`.
  - Tạo `control_station/tests/read_pt100.py`.
  - Tạo `control_station/tests/relay_sequence.py`.
  - Tạo `control_station/tests/temperature_relay_sequence.py`.
- **Nội dung chính:**
  - `ModbusRTU` sở hữu một serial client, cấu hình COM/9600 N81/timeout,
    tuần tự hóa giao dịch, retry và đóng kết nối. Bổ sung API đọc FC03/FC04,
    ghi FC06/FC16, đọc FC01 và ghi FC05/FC0F.
  - FC16 broadcast và FC03 broadcast-read chỉ dùng được khi caller cho phép
    tường minh; hỗ trợ dải slave ID EPCB 1..255.
  - Cấu hình bus hiện dùng COM14, 9600 N81, timeout 1000 ms, 10 retries và
    delay retry 200 ms. PT100 đặt ID vận hành 1, relay đặt ID vận hành 2;
    ID mặc định xuất xưởng của cả hai được ghi riêng là 255.
  - Register map PT100: nhiệt độ FC03 `0x0000`, điện trở `0x0001`, ID
    `0x0002` FC06, baud `0x0003` FC06, hiệu chỉnh nhiệt độ/điện trở
    `0x0004`/`0x0005`, gửi định kỳ `0x0006`.
  - Register map relay: trạng thái FC01 và coils `0x0000`/`0x0001` cho hai
    relay; điều khiển FC05/FC0F. Ghi cấu hình ID qua FC16 tại `0x0000`,
    cấu hình baud tại `0x03E9` và đọc baud tại `0x03E8`.
  - Các script kiểm tra là script chạy thủ công, không tự chạy trong unittest
    discovery. Có script đọc PT100 liên tục, test trình tự relay, và test chung:
    bật relay 1 đến khi PT100 đạt 70°C, tắt relay 1, chờ 2 giây rồi bật relay 2.
    Test chung có timeout gia nhiệt 5 phút, đọc mỗi 200 ms và thử tắt cả hai
    relay khi kết thúc.
- **Kiểm tra và kết quả:**
  - `test_modbus.py`: **8 test đạt** với fake client, gồm đọc/ghi, retry,
    slave ID và broadcast.
  - Đã kiểm tra parse YAML, compile các script và thứ tự giao dịch của test relay
    bằng fake client; raw PT100 `700` được giải mã thành `70.0°C`.
  - Trong lần chạy toàn bộ test trước đó, 3 test controller AUTO/MANUAL thất bại
    tại bước đồng bộ đầu ra; không thuộc nhóm test Modbus.
- **Ghi chú/phần chưa triển khai:** Chưa xác nhận các script chạy thành công trên
  phần cứng thật. Terminal trước đó nhận phản hồi từ ID 1 khi script hỏi ID khác,
  nên config hiện đặt PT100 ở ID 1; relay phải được cấu hình thực tế sang ID 2.
  Đổi ID trong YAML không tự đổi ID trong module. `main.py`, `pt100.py` và
  `relay.py` vẫn là khung, chưa nạp config hoặc nối Modbus vào controller.
  Ba test controller thất bại cần xử lý riêng; comment `retries` trong config
  cũng cần rà lại vì giá trị hiện là 10 nhưng comment mô tả tổng cộng 6 lần gửi.

## 9. Xác nhận kiểm thử relay trên phần cứng

- **Thời gian UTC+7:** 2026-10-03 (ghi bù; giờ hoàn thành không được ghi nhận).
- **Mục tiêu:** Ghi nhận kết quả kiểm tra điều khiển relay sau khi hiệu chỉnh
  cách diễn giải trạng thái đọc FC01.
- **File liên quan:** `control_station/tests/relay_sequence.py`,
  `control_station/config.yaml` và `docs/development_log.md`.
- **Nội dung chính:** Script relay dùng FC05 ghi riêng từng coil: bật/tắt relay 1
  tại coil `0x0000`, relay 2 tại coil `0x0001`; slave ID dùng trong lần kiểm tra
  là 1. Khi đọc FC01, cấu hình hiện tại diễn giải `false` là ON và `true` là OFF.
- **Kiểm tra và kết quả:** Người dùng xác nhận đã kiểm tra relay trên phần cứng
  và test thành công. Nhật ký phiên không lưu output chi tiết của lần chạy thành
  công hoặc số đo thời gian thực tế.
- **Ghi chú/phần chưa triển khai:** Xác nhận này chỉ áp dụng cho bài test relay;
  chưa xác nhận PT100, kịch bản ngưỡng nhiệt độ 70°C hoặc tích hợp vào vòng lặp
  `main.py`. Kịch bản `relay_sequence.py` vẫn thử tắt hai relay khi cleanup.


## 2026-09-28 — HMI cục bộ Flask và chế độ mô phỏng

- Triển khai `control_station/hmi/app.py` với API trạng thái, API lệnh và health check.
- Triển khai giao diện HTML/CSS/JavaScript dùng HTTP polling mỗi giây.
- HMI hỗ trợ OFF/MANUAL/AUTO, setpoint, hai đầu ra, reset FAULT và dừng khẩn.
- Thêm mô phỏng nhiệt và relay trong RAM để chạy khi chưa có phần cứng.
- Sửa lỗi cú pháp trong comment tại `controller.update_measurement` trên nhánh `hoang_dev`.
- Bổ sung 6 test HTTP; tổng cộng 48/48 test controller và HMI đạt.
- Chưa tích hợp HMI với vòng chạy Modbus thật trong `control_station.main`.


## 2026-10-03 — Rà soát và sửa lỗi tích hợp trên hoang_dev

- **Thời điểm commit UTC+7:** 2026-10-03 22:51:34 (theo metadata GitHub; không phải giờ bắt đầu sửa).
- **Nhánh nguồn:** `main`, commit `310c8adc91b9daf00a4fc121059dd108974f0581` (Modbus và các script phần cứng từ `loc1` đã merge vào `main`).
- **Nhánh thực hiện/nhận bản sửa:** `hoang_dev`, dựa trên commit `37e6d5b1f699cb124647fd013c701545de146b6c`, giữ HMI và controller của nhánh này.
- **Commit kết hợp và sửa lỗi:** `c720d1ac2ddaeb95dd0694d6a50cf5199cd2e05e`.
- **Bổ sung thông tin nhật ký:** theo yêu cầu lúc 2026-10-03 22:58:17 UTC+7; chỉ cập nhật tài liệu trên `hoang_dev`.

- Kết hợp Modbus và script thiết bị từ main với HMI của hoang_dev.
- Giữ đồng bộ OFF chỉ khi bus vừa kết nối; sửa lỗi thụt lề trên main.
- Retry phản hồi thiếu dữ liệu; không retry exception thiết bị mã 1/2/3.
- Không để lỗi đóng COM ngắt retry; giữ stopbits 1.5 và kiểm tra kiểu register.
- Thống nhất giải mã bit relay, dùng ID YAML mặc định, đồng bộ OFF trước test relay và luôn đóng COM.
- Giảm retry cấu hình xuống 2; giữ timeout 1000 ms cần xác minh trên thiết bị.
- Chưa chạy script phần cứng hoặc triển khai các module còn là khung.

- Kiểm tra sau sửa: 63/63 unit test đạt (42 controller, 6 HMI, 8 Modbus, 7 regression); parse cú pháp toàn bộ Python đạt. Không thử phần cứng thật.

## 2026-10-09 — Driver PT100 và test ngắt tải NC tại 60°C

- **Thời điểm ghi nhật ký UTC+7:** 2026-10-09 13:55:21; đây là thời điểm ghi nhật ký, không phải thời điểm commit.
- **Nhánh thực hiện:** `hoang_dev`, dựa trên `75a3da9`; không lấy code từ nhánh khác trong lần cập nhật này.
- **File thay đổi:** `control_station/pt100.py`, `control_station/config.yaml`,
  `control_station/modbus.py`, `control_station/tests/read_pt100.py`,
  `control_station/tests/temperature_relay_sequence.py`,
  `control_station/tests/nc_temperature_cutoff.py`,
  `control_station/tests/test_pt100.py`, `README.md`, `docs/development_log.md`.
- **Nội dung và lý do:**
  - Triển khai driver PT100 dùng kết nối Modbus chung, đọc FC03, giải mã int16/uint16
    và scale, kiểm tra phản hồi và giới hạn nhiệt độ. Khi lỗi không trả mẫu cũ.
  - Các script đọc PT100 và test nhiệt độ/relay dùng driver chung, giữ hàm giải mã
    có thể import để tương thích với code hiện có.
  - Thêm bài test thủ công cho hai tải COM-NC: đọc cảm biến trước khi cấp điện,
    chạy khi dưới 60°C, bật cuộn relay để mở NC/ngắt tải khi đạt ngưỡng.
    Giữ cuộn relay ON khi kết thúc, timeout hoặc Ctrl+C; thử ngắt cả hai kênh
    khi có lỗi. Mất nguồn module relay sẽ đóng NC trở lại.
  - Ghi nhận thay đổi cấu hình của người dùng: COM14 thành COM5, PT100 ID 1
    thành 2, relay ID 2 thành 1; bổ sung giới hạn nhiệt độ -200..850°C.
  - Bổ sung chú thích retry/delay trong Modbus và hướng dẫn driver trong README.
- **Kiểm tra:** `python -X utf8 -m unittest discover -s control_station/tests -q`:
  **69/69 test đạt**. `git diff --check` đạt. Bài test NC đã kiểm tra bằng bus giả
  cho ngưỡng 60°C, cảm biến đã nóng lúc bắt đầu và lỗi cảm biến khi cleanup.
- **Giới hạn:** Chưa xác nhận phần cứng trong phiên này. Chưa gán riêng quạt/gia
  nhiệt cho relay 1/2; test NC ngắt cả hai cùng lúc. `main.py` và driver relay vẫn
  chưa tích hợp. Số đo trong dải hợp lệ chưa đủ chứng minh cảm biến còn nối dây.

## Quy ước ghi nhật ký cho các lần sửa tiếp theo

Mỗi mục mới ghi ngày và giờ UTC+7, nhánh thực hiện, nhánh nguồn nếu lấy code từ nhánh khác, commit nguồn khi có, các file thay đổi, lý do và kết quả kiểm tra. Phân biệt thời điểm ghi nhật ký với thời điểm commit; không suy đoán giờ cho các mốc cũ chưa có bằng chứng.

## Nhật ký nguồn nhập từ loc1 tại 2872de2

Mục dưới được giữ nguyên từ nhánh nguồn khi tích hợp. Các con số test và phần
chưa triển khai mô tả thời điểm cũ; kết quả bản tích hợp nằm ở mục tiếp theo.

## 10. Bổ sung adapter PT100 và thao tác đổi slave ID

- **Thời gian UTC+7:** 2026-10-04 (ghi bù; giờ hoàn thành không được ghi nhận).
- **Mục tiêu:** Hoàn thiện module PT100 theo cấu hình, thêm thao tác đổi ID tường
  minh và dùng chung logic đọc/giải mã trong các script kiểm tra.
- **File được sửa:**
  - `control_station/pt100.py`.
  - `control_station/tests/test_pt100.py`.
  - `control_station/tests/read_pt100.py`.
  - `control_station/tests/temperature_relay_sequence.py`.
- **Nội dung chính:**
  - `PT100Sensor` đọc FC03 qua `ModbusRTU` dùng chung, giải mã `int16`/`uint16`,
    áp dụng `scale`, đồng thời cung cấp raw register và nhiệt độ °C qua một lần
    đọc.
  - Thêm `change_slave_id()`: dùng `current_slave_id` làm ID nguồn, ghi
    `slave_id` đích vào `device_id_register` bằng FC06. Không ghi nếu ID nguồn
    đã trùng ID đích; thao tác chỉ chạy khi được gọi tường minh.
  - `read_pt100.py --change-id` gọi phương thức mới; `--current-id` tùy chọn
    ghi đè ID nguồn trong YAML. Script chuỗi PT100/relay dùng `PT100Sensor`
    thay vì giữ bản giải mã nhiệt độ trùng lặp.
  - Cấu hình lúc ghi log đặt PT100 `current_slave_id: 1`, `slave_id: 2`;
    `device_id_register` là `0x0002`. Cấu hình relay cũng đang đặt
    `slave_id: 1`; cần xác nhận ID thiết bị thực tế và tránh trùng ID trên bus
    trước khi gửi lệnh đổi ID cho phần cứng.
- **Kiểm tra và kết quả:**
  - `test_pt100.py` và `test_modbus.py`: **20/20 test đạt**, gồm kiểm tra lệnh
    FC06 dùng register `0x0002`, giá trị đích `2` và ID nguồn `1`.
  - Mô phỏng bằng cấu hình YAML xác nhận giao dịch ghi có dạng
    `(register=2, value=2, source_id=1)`.
  - Toàn bộ 62 test: **59 đạt, 3 thất bại** trong các kiểm thử hysteresis AUTO
    và điều khiển MANUAL của `test_controller.py`; các lỗi này không thuộc nhóm
    thay đổi PT100/Modbus.
- **Ghi chú/phần chưa triển khai:** Chưa gửi lệnh đổi ID hoặc xác minh đọc nhiệt
  độ trên phần cứng thật. `main.py` vẫn chưa nối PT100 vào vòng điều khiển.

## 2026-10-11 — Tích hợp logic gia nhiệt và quạt đồng bộ toàn máy 1

- **Thời điểm ghi nhật ký UTC+7:** 2026-10-11 20:40:57; đây là thời điểm ghi nhật ký, không phải thời điểm commit.
- **Nhánh thực hiện:** hoang_dev, dựa trên 6f952e71796503fb335b9312f133097387b3a494.
- **Nhánh nguồn:** loc1 tại 2872de228b0fd528b7977cca76990628be0097f0. Commit tích hợp giữ cả hai nhánh nguồn làm parents.
- **File thay đổi (29):** `Proposed_changes.txt`, `README.md`, `control_station/.env.example`, `control_station/__init__.py`, `control_station/config.yaml`, `control_station/configuration.py`, `control_station/controller.py`, `control_station/hmi/app.py`, `control_station/hmi/static/app.js`, `control_station/hmi/templates/index.html`, `control_station/main.py`, `control_station/modbus.py`, `control_station/models.py`, `control_station/pt100.py`, `control_station/relay.py`, `control_station/runtime.py`, `control_station/tests/helpers.py`, `control_station/tests/nc_temperature_cutoff.py`, `control_station/tests/read_pt100.py`, `control_station/tests/relay_sequence.py`, `control_station/tests/temperature_relay_sequence.py`, `control_station/tests/test_controller.py`, `control_station/tests/test_hmi_app.py`, `control_station/tests/test_pt100.py`, `control_station/tests/test_relay.py`, `control_station/tests/test_review_regressions.py`, `control_station/tests/test_runtime_integration.py`, `docs/communication_contract.md`, `docs/development_log.md`.
- **Nội dung và lý do:**
  - Tích hợp bốn pha AUTO: gia nhiệt trước, chạy cùng quạt, dừng gia nhiệt rồi quạt chạy tiếp; giữ hysteresis và cơ chế lỗi quạt/retry.
  - Dùng process_fan thống nhất trong models, controller, relay, HMI, JSON/telemetry, test và tài liệu; bỏ bí danh/lệnh cooling_fan cũ.
  - Thêm configuration.py kiểm tra YAML; ControlSettings không có bộ mặc định vận hành riêng. Thời gian 5 s/10 s và timeout quạt được nạp cho cả mô phỏng và phần cứng. Retry/delay Modbus cũng lấy từ cấu hình.
  - Triển khai main.py và StationRuntime dùng một bus chung: đọc PT100, cập nhật mẫu, chạy controller, giữ FAULT khi reconnect và tắt tải/đóng bus khi dừng.
  - Thống nhất PT100Sensor, giữ kiểm tra phản hồi/giới hạn từ hoang_dev và thao tác đổi ID tường minh từ loc1; không dùng mẫu cũ khi lỗi.
  - RelayModule chuyển trạng thái tải sang coil theo NO/NC, đọc lại xác nhận, thử cả hai tải khi cleanup. Cấu hình hiện giữ COM5, PT100 ID 2, relay ID 1; NO/NC phải khớp cách đấu dây thực tế.
  - HMI hiển thị pha AUTO, thời gian cấu hình và quạt tuần hoàn; mô phỏng dùng cùng controller/cấu hình. Các script đọc cảm biến, AUTO, NC và chẩn đoán relay được cập nhật theo API chung.
  - Giữ các sửa lỗi Modbus/readback/cleanup trước đây; gộp nhật ký nguồn và cập nhật README, hợp đồng JSON, quyết định logic.
- **Kiểm tra:** 93/93 unit và integration test đạt; có ca YAML thay 3 s/7 s thực sự thay timer và phản hồi HMI, MANUAL đồng thời, lỗi quạt, cảm biến sai, bus mất/reconnect, NO/NC, shutdown và đổi ID tường minh.
  Parse toàn bộ Python, Ruff kiểm tra lỗi F, Node kiểm tra cú pháp JS, sáu CLI --help và git diff --check đạt.
  HMI mô phỏng chạy bằng entry point thật: kiểm tra HTTP, MANUAL hai tải ON, emergency stop/reset; frontend render payload HTTP và thời gian cấu hình đạt; subprocess dừng sạch với exit 0.
  Môi trường kiểm thử: Python 3.12, Flask 3.1.3, pymodbus 3.16.0, PyYAML 6.0.3; không mở phần cứng thật.
- **Giới hạn:** Chưa xác nhận PTC, quạt và relay thật. Readback coil không xác nhận tải vật lý; tải NC có thể bật khi module mất nguồn. Timeout quạt được xử lý sau lời gọi Modbus đồng bộ. Chưa thêm recipe giữ nhiệt/kết thúc mẻ, ngưỡng nguội, cửa thoát hơi, MQTT/database/SCADA.

## 2026-10-11 — Mẻ sấy theo thời gian giữ nhiệt, cài đặt trên HMI

- **Thời điểm ghi nhật ký UTC+7:** 2026-10-11 21:21:54; thời điểm ghi log, không phải giờ commit.
- **Nhánh thực hiện:** `hoang_dev`, dựa trên `6b4795df5316834f5216b81adf5c60c7b83c72b0`.
- **Nguồn thay đổi:** yêu cầu người dùng chốt gia nhiệt → giữ nhiệt đủ thời gian →
  làm nguội → hoàn tất, thời gian nhập trên HMI. Bản nền là commit tích hợp
  `loc1@2872de228b0fd528b7977cca76990628be0097f0` vào `hoang_dev`; lần này không
  lấy thêm code từ nhánh khác. `main` và `loc1` không được cập nhật.
- **File thay đổi (15):** `Proposed_changes.txt`, `README.md`,
  `control_station/config.yaml`, `control_station/models.py`,
  `control_station/controller.py`, `control_station/hmi/static/app.js`,
  `control_station/hmi/static/style.css`, `control_station/hmi/templates/index.html`,
  `control_station/tests/helpers.py`, `control_station/tests/temperature_relay_sequence.py`,
  `control_station/tests/test_controller.py`, `control_station/tests/test_drying_cycle.py`,
  `control_station/tests/test_runtime_integration.py`, `docs/communication_contract.md`,
  `docs/development_log.md`.
- **Thay đổi và lý do:**
  - Thay vòng giữ nhiệt lặp vô hạn bằng IDLE/HEATING/HOLDING/COOLING/COMPLETE.
    HEATING lên setpoint; giữ trễ bật quạt trong YAML. HOLDING quạt ON liên tục,
    heater đóng/ngắt theo S-D/S. Đủ thời gian giữ nhiệt mới vào COOLING;
    heater không bật lại, fan ON đến ngưỡng nguội rồi COMPLETE/tắt hai tải.
  - `drying_duration_s` mặc định 600 s (10 phút), `cooling_stop_temperature_c`
    mặc định 35 °C. Bỏ `fan_post_run_s`; làm nguội theo nhiệt độ thay vì 10 s.
    Vẫn giữ hạn xác nhận quạt riêng `fan_start_confirmation_timeout_s`.
  - Bộ đếm bắt đầu sau khi đạt setpoint và xác nhận quạt ON. Chỉ cộng khoảng giữa
    hai mẫu liên tiếp trong [S-D, S+D], bỏ khoảng chuyển tiếp ra/vào vùng. Ngoài
    vùng tạm dừng nhưng giữ tiến độ. Thời gian gia nhiệt/nguội không tính là sấy.
  - HMI nhập phút, gửi giây qua SET_DRYING_DURATION. Có SET_COOLING_TEMPERATURE,
    START_DRYING, hiển thị pha/đã giữ nhiệt/còn lại/đang đếm hoặc tạm dừng.
    Giữ giá trị chưa lưu khi polling; không cho bắt đầu khi còn thông số chưa lưu.
    Khóa chỉnh thông số và từ chối bắt đầu lại trong mẻ. COMPLETE chờ lệnh mẻ mới.
    Chỉnh thông số sau COMPLETE đưa về OFF/sẵn sàng, xóa tiến độ mẻ cũ.
  - HMI chỉnh thông số trong RAM phiên chạy; không ghi ngược YAML. Khởi động lại
    lấy mặc định YAML. Ngưỡng nguội phải >= 0 và thấp hơn S-D; validate số hữu hạn,
    không bool, thời gian > 0, vùng giữ nhiệt dưới giới hạn quá nhiệt.
  - OFF/MANUAL, lỗi, shutdown/restart hủy mẻ và xóa bộ đếm; reset không tiếp tục
    mẻ cũ. Quạt lỗi/quá hạn ngắt heater rồi thử lại một lần; retry xác nhận được
    quạt thì heater giữ OFF ở chu kỳ đó, chỉ bật lại ở chu kỳ an toàn sau nếu
    còn HEATING. Hai lần lỗi chốt FAULT/hủy mẻ.
  - Script AUTO chạy cùng controller/runtime đến COMPLETE; --drying-minutes
    chỉnh thời gian sấy, --max-wait-seconds giới hạn toàn bộ mẻ. Hết hạn chưa
    hoàn tất báo thất bại và cleanup. HMI/script nhắc mở cửa thoát ẩm thủ công.
  - Cập nhật models/TelemetryRecord, schema cục bộ 3, README và quyết định logic.
- **Kiểm tra:**
  - `python -X utf8 -m unittest discover -s control_station/tests -q`:
    **115/115 test đạt**, gồm 21 test mẻ sấy mới và integration qua HMI/bus giả.
    Kiểm tra đếm đúng, tạm dừng/đếm tiếp, biên vùng, khóa recipe, giữ quạt,
    heater không bật lại khi nguội, COMPLETE không tự restart, hủy/reset/restart,
    lỗi relay/bus và thời gian dạng thập phân.
  - Live HTTP qua launcher mô phỏng chạy đủ HEATING/HOLDING/COOLING/COMPLETE;
    quan sát pause ngoài vùng, kiểm tra chỉnh thời gian/khóa khi chạy, COMPLETE
    chốt và mẻ mới tường minh. Ctrl+C thoát 0, dừng thread và đầu ra.
  - Frontend JS render các payload HTTP thật, trạng thái/bộ đếm/nhắc cửa/khóa
    thông số; kiểm tra polling giữ input chưa lưu và 2.5 phút gửi 150 giây.
  - Parse AST 29 file Python, Ruff F, JS syntax, 6 CLI --help và git diff --check đạt.
- **Giới hạn:** Chưa mở COM hoặc thử thiết bị thật. Bộ đếm dựa trên các mẫu đo,
  không chứng minh nhiệt luôn trong vùng giữa hai mẫu. Readback coil chưa là
  phản hồi quạt quay; timeout vẫn kiểm tra sau giao dịch đồng bộ. Chưa có driver
  cửa; mở cửa thủ công. Ngưỡng nguội dưới môi trường có thể không đạt. Thời gian
  và nhiệt độ là thông số thực nghiệm, chưa có đo ẩm/mô hình để xác nhận giấy khô.
  MQTT/database/SCADA từ xa chưa triển khai.

## 2026-10-11 — Bỏ các thông tin HMI theo ảnh người dùng chọn

- **Thời điểm ghi nhật ký UTC+7:** 2026-10-11 22:26:01; là giờ ghi log, không phải giờ commit.
- **Nhánh thực hiện và nguồn:** `hoang_dev`, dựa trên `48b9bd89807f2391c349ffb1a70292f2b7b38ecf`;
  không lấy code từ nhánh khác.
- **Phạm vi người dùng yêu cầu:** bỏ khối Pha AUTO / Trễ bật quạt / Vùng giữ nhiệt
  trong ảnh đính kèm, bỏ Độ trễ bật lại và bỏ 12 V khỏi tên HMI.
- **File thay đổi:** `control_station/hmi/templates/index.html`,
  `control_station/hmi/static/app.js`, `control_station/hmi/static/style.css`,
  `control_station/tests/test_hmi_app.py`, `docs/development_log.md`.
- **Nội dung:** bỏ các phần tử HTML đã chọn và các dòng JavaScript cập nhật chúng;
  tên hiển thị thành Buồng sấy giấy; bố cục metadata mobile còn hai cột;
  cập nhật chuỗi tên trong test HMI hiện có.
- **Không thay đổi:** controller, runtime, drivers, YAML, deadband, trễ bật quạt,
  bộ đếm và quy tắc sấy. Đây là thay đổi hiển thị theo yêu cầu.
- **Kiểm tra:** 6/6 test HMI đạt; frontend render các pha, khóa thông số,
  giữ input chưa lưu và chuyển phút sang giây đạt; node --check và git diff --check đạt.

## 2026-10-12 — HMI công nghiệp và sơ đồ SVG động đã được người dùng chốt

- **Thời điểm ghi nhật ký UTC+7:** 2026-10-12 11:19:47; là giờ ghi log, không phải giờ commit.
- **Nhánh thực hiện và nguồn:** `hoang_dev`, dựa trên `2c669f508852e3b85bf11c4a82c25fb068ddb70e`;
  không lấy code từ nhánh khác. Tại lúc bắt đầu, `loc1` cũng trỏ đến commit này.
- **Phạm vi chốt:** bố cục HMI trung tính có hình buồng sấy; quạt sát gia nhiệt,
  module relay ở ngoài; quạt/quá trình tuần hoàn có chuyển động, gia nhiệt có hiệu
  ứng khi ON; trạng thái bám phản hồi đã xác nhận; ô kết nối Relay thay Modbus.
  Giữ chức năng/logic sấy, không thêm điều khiển cửa hoặc đo ẩm.
- **File thay đổi (12, đều là file đã có):** `control_station/hmi/templates/index.html`,
  `control_station/hmi/static/style.css`, `control_station/hmi/static/app.js`,
  `control_station/hmi/app.py`, `control_station/relay.py`, `control_station/runtime.py`,
  `control_station/main.py`, `control_station/tests/test_hmi_app.py`,
  `control_station/tests/test_relay.py`, `control_station/tests/test_runtime_integration.py`,
  `docs/communication_contract.md`, `docs/development_log.md`.
- **Giao diện:** tách giám sát/cài đặt, cảnh báo trên cùng, kết nối phía dưới;
  nền xám, nút lệnh rõ ràng; BẬT/TẮT riêng cho mỗi tải, giữ lệnh bool hiện có.
  Giữ OFF/MANUAL/AUTO, ba thông số mẻ, nút bắt đầu, bộ đếm, dừng khẩn/reset.
  Các mục pha AUTO/trễ bật quạt/vùng giữ nhiệt/deadband và 12 V đã bỏ không trở lại.
- **Sơ đồ:** vẽ trực tiếp SVG trong HTML, không thêm ảnh nền tĩnh hay file asset.
  Quạt quay bằng CSS; mũi tên tuần hoàn dịch nét đứt; PTC có hiệu ứng nóng độc lập.
  PT100 cập nhật nhiệt độ; module relay hiển thị trạng thái hai kênh tải.
  Không bật hiệu ứng theo thao tác bấm nút; dùng readback cache đã xác nhận.
  Mất phản hồi relay hoặc GET HMI lỗi/timeout thì dừng hiệu ứng, hiện chưa xác nhận;
  có dữ liệu mới thì phục hồi. Giữ input chưa lưu và điều kiện khóa khi AUTO/FAULT.
  Hỗ trợ màn hình máy tính/điện thoại và prefers-reduced-motion.
- **Dữ liệu cần cho ô Relay:** thêm communication_status/cache readback tại driver;
  runtime probe mỗi giây trên bus chung, dùng một FC01 cho hai coil liên tiếp.
  HMI nhận driver qua create_app và chỉ đọc cache, GET không gửi Modbus. Payload
  bổ sung state.relay_connected và relay_feedback với bool/null đã xét NO/NC.
  Demo có nhãn MÔ PHỎNG và dùng trạng thái relay RAM. Probe lỗi chỉ cập nhật kết
  nối hiển thị, không thay đổi chính sách FAULT của controller. Timeout/retry
  vẫn như cấu hình hiện có; giao dịch có thể kéo dài hơn chu kỳ kiểm tra.
- **Giữ nguyên:** `controller.py`, `models.py`, `config.yaml`, `modbus.py`,
  `pt100.py` không thay đổi. Các mặc định thời gian/nhiệt độ, cách khởi động
  mô phỏng/phần cứng và thuật toán sấy không đổi trong lần này.
- **Kiểm tra:**
  - **124/124 test đạt** (115 test hiện có, 9 test bổ sung tại các file test đã có).
    Bao gồm relay lỗi/phục hồi, độc lập với PT100/bus, cache không gây IO từ GET,
    readback khác trạng thái lệnh, từ chối lệnh không giả định tải đổi trạng thái.
  - Chromium headless chạy HMI từ launcher thật qua HTTP: bấm MANUAL và BẬT/TẮT
    từng tải; xác minh ma trận quay cánh quạt thay đổi, shader gia nhiệt độc lập,
    lệnh bị từ chối không bật/tắt hình, relay mất phản hồi và mất HTTP dừng hiệu
    ứng, phục hồi khi có dữ liệu; nhập 20 phút gửi 1200 giây, giữ input chưa lưu;
    AUTO khóa thông số/điều khiển tay; dừng khẩn và reset đúng.
  - Rà ảnh render thật ở 1440 px, 1280x900 và mobile 390 px; không tràn ngang,
    bố cục desktop 1280x900 vừa một màn hình. Không có pageerror; Ctrl+C thoát 0.
  - Python AST, node --check và git diff --check đạt. Không mở COM/thiết bị thật.
- **Giới hạn:** hiệu ứng là biểu diễn trạng thái tải suy ra từ coil, chưa đo quạt
  quay hay nhiệt của PTC. Hình bố trí minh họa; cửa thoát ẩm vẫn mở thủ công.
  Không khẳng định chứng nhận tuân thủ toàn bộ ISA-101 từ bố cục này.

## 2026-10-21 — Làm rõ vai trò client/server giữa máy 2 và ThingsBoard

- **Thời điểm ghi nhật ký UTC+7:** 2026-10-21 00:01:29.
- **Mục tiêu:** Giải thích đường giao tiếp và phân biệt vai trò client/server
  giữa máy 1, máy 2, ThingsBoard và trình duyệt SCADA theo hợp đồng hiện có.
- **File thay đổi:** `docs/development_log.md` (chỉ ghi nhật ký; không sửa mã
  hoặc hợp đồng giao tiếp).
- **Nội dung chính:**
  - Làm rõ máy 2 không giao tiếp HTTP trực tiếp với máy 1; hai máy trao đổi dữ
    liệu/lệnh gián tiếp qua ThingsBoard.
  - Backend SCADA trên máy 2 là client khi gọi REST/HTTPS và mở WebSocket tới
    ThingsBoard; ThingsBoard là server ở kết nối đó.
  - Máy 1 là client MQTT/TLS kết nối tới ThingsBoard; ThingsBoard là server
    MQTT và trung gian nhận telemetry/chuyển RPC.
  - Backend Flask trên máy 2 là server đối với trình duyệt SCADA, còn trình
    duyệt là client gọi API của backend.
- **Căn cứ:** `docs/communication_contract.md` mô tả máy 1 dùng MQTT/TLS,
  backend máy 2 dùng REST/WebSocket với ThingsBoard và frontend chỉ gọi backend
  máy 2; `remote_scada/thingsboard_client.py` triển khai phiên HTTP và
  WebSocket.
- **Kiểm tra:** Đối chiếu mục “Telemetry và quyền từ xa” trong hợp đồng giao
  tiếp với cách triển khai client ThingsBoard; đây là giải thích kiến trúc, không
  thay đổi luồng chạy hay kiểm thử.
- **Giới hạn:** Mô tả phản ánh kiến trúc đã định nghĩa trong tài liệu và mã
  nguồn; kết nối thực tế vẫn cần cấu hình ThingsBoard và xác nhận khi vận hành.

## 2026-10-21 — Chẩn đoán relay EPCB và thêm menu kiểm tra tương tác

- **Thời điểm ghi nhật ký UTC+7:** 2026-10-21 09:45:43; là giờ ghi nhật ký,
  không phải thời điểm commit.
- **Mục tiêu:** Đối chiếu giao thức relay EPCB với frame người dùng cung cấp,
  sửa cách đọc trạng thái coil, và giúp chạy dò/đọc/test relay từ một menu.
- **File thay đổi:** `README.md`, `control_station/config.yaml`,
  `control_station/relay.py`, `control_station/tests/relay_sequence.py`,
  `control_station/tests/test_relay.py`,
  `control_station/tests/test_review_regressions.py`,
  `control_station/tests/test_runtime_integration.py`,
  `control_station/tests/test_mqtt_client.py`.
- **Thay đổi và lý do:**
  - Đặt `relay.status_coil_count` thành 8 theo frame FC01 đọc từ địa chỉ 0,
    số lượng 8; giữ mapping relay 1/2 lần lượt ở coil 0/1, tương ứng bit 0/1
    trong phản hồi.
  - Cập nhật `RelayModule` và script phần cứng để FC01 luôn đọc block trạng
    thái 8 coil từ coil đầu tiên; thao tác ghi FC05 vẫn ghi riêng coil đã chọn.
  - Cấu hình readback hiện dùng `false=OFF`, `true=ON`, phù hợp với kết quả
    test relay 1 đã quan sát được sau khi đổi quy ước.
  - Khi chạy `relay_sequence` không có tham số, hiển thị menu `doc`,
    `docrelay1-2`, `battat`, `thoat`. `doc` dò ID/baud; `docrelay1-2` chỉ đọc
    trạng thái; `battat` chạy chuỗi bật/tắt từng relay và cleanup gửi OFF.
  - Cập nhật bus giả và kiểm thử liên quan để phản ánh 8 bit readback và quy
    ước raw hiện tại; cập nhật README hướng dẫn sử dụng và cảnh báo thao tác
    phần cứng.
- **Căn cứ phần cứng đã quan sát:** Ghi ID bằng FC16 broadcast và đọc lại qua
  broadcast cho giá trị 1; dò baud đọc được register `0x03E8`, code 3/9600 ở
  slave 1. FC01 count 1 tại địa chỉ 0 đã trả `[False]`, còn đọc địa chỉ 1
  riêng và đọc count 2 đã timeout. Sau khi đổi mapping raw, bài test riêng
  relay 1 chạy OFF → ON → OFF và xác nhận readback từng bước. Các quan sát này
  dẫn đến việc dùng frame FC01 count 8 theo datasheet thay vì suy ra relay 2
  hỏng từ yêu cầu count 1/2.
- **Kiểm tra:** Toàn bộ 133 test của `control_station/tests` đạt; các nhóm
  relay adapter, relay regression, runtime integration và MQTT đều đạt. Menu
  interactive được smoke-test bằng lệnh `thoat`, không kết nối phần cứng.
- **Giới hạn:** Sau khi đổi FC01 sang count 8, chưa chạy lại `docrelay1-2` trên
  thiết bị thật; vì vậy chưa xác nhận readback hai relay trên hardware. Test
  ON/OFF relay 1 trước thay đổi count 8 không thay thế xác nhận này. Tải cần
  được cách ly khi chạy `battat`.
- **Chẩn đoán SCADA trong cùng phiên:** Flask tại `127.0.0.1:8080` khởi động;
  luồng WebSocket telemetry ThingsBoard lặp cảnh báo `ThingsBoardError`.
  Kiểm tra cấu hình chỉ xác nhận các biến cần thiết hiện diện và URL dùng
  HTTPS; không đọc/in giá trị bí mật. `TB_DEVICE_ID` không có hình dạng UUID,
  nên cần thay bằng UUID device ID trong ThingsBoard rồi thử lại. Chưa xác
  nhận được đăng nhập ThingsBoard hoặc nhận telemetry; log hiện tại chưa cho
  biết HTTP status/lý do từ chối chính xác.
