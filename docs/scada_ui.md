# SCADA buồng sấy giấy

## Chạy xem thử khi chưa có thiết bị

Từ thư mục gốc repository, sau khi cài các dependencies của máy 1 và SCADA:

```powershell
python -m remote_scada.preview
```

Mở http://127.0.0.1:8081 và đăng nhập **operator / demo-scada**.

Launcher này chỉ bind localhost, dùng controller và mô hình nhiệt của dự án.
Không đọc `.env`, không mở COM và không kết nối ThingsBoard. Dữ liệu lịch sử
nền là dữ liệu minh họa, được ghi rõ trên giao diện; các lệnh tiếp theo thao
tác controller mô phỏng. Dữ liệu chỉ giữ trong RAM, restart khởi động OFF.
Mỗi lần khởi động preview tạo session key mới.

Để kiểm tra hai máy qua ThingsBoard, tiếp tục dùng
`python -m control_station.main --mqtt` ở máy 1 và
`python -m remote_scada.server` ở máy 2 với `.env` hiện có.
**Cập nhật cả hai máy cùng phiên bản**, vì SCADA mới gửi `SET_RECIPE`.
Ctrl+F5 trên trình duyệt để nạp JavaScript/CSS mới.

## Bốn màn hình

| Màn hình | Nội dung |
| --- | --- |
| Vận hành | Nhiệt độ PV, điểm đặt SP, vùng giữ nhiệt, sơ đồ buồng sấy, lệnh đầu ra và feedback relay, tiến độ mẻ, nhập thông số, bắt đầu/dừng, yêu cầu trong phiên |
| Xu hướng | Lịch sử nhiệt độ/điểm đặt, trục thời gian và °C, xem mẫu đo khi di chuột, chọn 1/6/24/168 giờ, tải lại, xuất CSV UTC |
| Cảnh báo | Lỗi hiện tại, mức độ, thời điểm, hướng xử lý, lịch sử sự kiện phát sinh/kết thúc và bộ lọc |
| Chẩn đoán | API SCADA, độ mới telemetry, MQTT/Modbus/PT100/relay, timestamp máy 1, tuổi dữ liệu và lỗi đọc API |

Điều hướng giữ cảnh báo chung và trạng thái dữ liệu ở vị trí cố định. Giao
diện dùng nền trung tính; màu đỏ dành cho lỗi và chức năng dừng khẩn. Thiết
bị có chữ trạng thái và sự khác biệt độ sáng, không dùng màu làm dấu hiệu
duy nhất. Không quay biểu tượng quạt vì chưa có cảm biến xác nhận tốc độ.
Nhãn MÔ PHỎNG tiếp tục hiển thị trên màn hình nhỏ.

Thanh đầu trang và điều hướng dùng xanh navy; PV, nút thao tác và pha đang
chạy dùng xanh dương. Kết nối hoặc feedback ON đã xác nhận dùng xanh lá,
OFF dùng xám, dữ liệu chưa xác nhận dùng xám/vàng theo ngữ cảnh. Màu xanh
trên biểu tượng thiết bị biểu diễn lệnh ON của controller; nhãn phản hồi
relay có màu và giá trị riêng, không suy ra phản hồi vật lý từ màu biểu tượng.

## Dữ liệu và thao tác

- Dữ liệu còn mới khi đọc được API và tuổi snapshot không quá 5 giây. Tuổi
  dữ liệu tiếp tục tăng theo đồng hồ đơn điệu ở trình duyệt khi GET đang chờ.
- Dữ liệu cũ/lỗi API làm nhiệt độ, tiến độ và phản hồi thiết bị chuyển sang
  chưa xác nhận; khóa sửa thông số và lệnh vận hành thông thường. Cảnh báo
  cuối được giữ với nhãn "gần nhất", không mặc định rằng nguyên nhân đã hết.
- Yêu cầu dừng khẩn vẫn có thể gửi khi dữ liệu chưa xác nhận hoặc đang chờ
  lệnh thường. Đây là yêu cầu qua cloud, vẫn phụ thuộc kết nối và controller;
  giao diện không tuyên bố tải đã dừng chỉ từ kết quả RPC.
- Bản nhập được giữ sau khi rời ô và khi polling. Nút Hoàn tác lấy lại bộ
  thông số của snapshot mới nhất. Bản nhập chưa áp dụng chặn bắt đầu mẻ.
- Áp dụng thông số gửi một RPC `SET_RECIPE`. Controller xác thực đủ ba giá
  trị và toàn bộ liên động dưới cùng khóa, rồi mới thay settings. Lỗi bất kỳ
  giữ nguyên cả bộ. Không tự quay về ba RPC cũ nếu controller chưa hỗ trợ.
- Sau accepted, UI giữ bản nhập và chờ telemetry trùng đủ ba giá trị. Sau
  15 giây chưa khớp, hiển thị chưa xác nhận và giữ bản nhập. Không tự gửi lại
  lệnh. Kết quả accepted không phải bằng chứng tải hoạt động.
- Danh sách 20 yêu cầu chỉ phục vụ phiên trình duyệt, không phải nhật ký
  kiểm toán bền vững. Lịch sử cảnh báo vẫn lấy từ ThingsBoard.
- Cảnh báo có sự kiện phát sinh/kết thúc; chưa bổ sung trạng thái ACK riêng
  vào giao thức. Không tạo nút ACK giả hoặc cho reset FAULT từ SCADA. Reset
  vẫn thực hiện tại HMI máy 1 sau khi controller xác nhận đủ điều kiện.

Biểu đồ ngắt nét khi khoảng cách mẫu lớn hơn ba khoảng tổng hợp, không biến
null thành 0. Giới hạn trục tung tính cả PV và SP. Dải giữ nhiệt là thông số
hiện tại của snapshot còn mới, không tái dựng thông số lịch sử. Hủy truy vấn
khi đổi khoảng ở trình duyệt không bảo đảm truy vấn backend đã dừng.

## Kiểm tra

```powershell
python -X utf8 -m unittest discover -s control_station/tests -p "test_*.py"
python -X utf8 -m unittest discover -s remote_scada/tests -p "test_*.py"
python -X utf8 -m remote_scada.tests.check_browser
```

Bài kiểm tra cuối cần Chrome hoặc Edge trên Windows, chạy headless với
profile tạm và server preview localhost. Kiểm tra đăng nhập, bản nhập qua
polling, SET_RECIPE, chặn nhấn lặp, khóa khi AUTO, tải lịch sử độc lập, dữ liệu
cũ/mất API/khôi phục, dừng khẩn, bốn màn hình và chiều rộng mobile. Chụp ảnh
vào `artifacts/scada/` (không theo dõi bằng Git), đóng browser/server/plant
sau khi kiểm tra. Các test không xác nhận kết nối ThingsBoard thật hoặc tải
vật lý.

## Cơ sở thiết kế

Đây là thiết kế cho dự án theo các nguyên tắc công khai về HMI và khả năng
vận hành; không phải tuyên bố chứng nhận phù hợp một tiêu chuẩn.

- [ISA-101: phạm vi thiết kế và vòng đời HMI](https://www.isa.org/standards-and-publications/isa-standards/isa-101-standards).
- [ISA-18: quản lý cảnh báo](https://www.isa.org/standards-and-publications/isa-standards/isa-18-series-of-standards).
- [HSE: thiết kế phòng điều khiển và phản hồi thao tác](https://www.hse.gov.uk/comah/sragtech/techmeascontrol.htm).
- [W3C: tương phản chữ](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)
  và [vùng bấm](https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced).
