# Thời gian phản hồi của hệ thống

## Các phần đã tối ưu

| Đường xử lý | Trước | Hiện tại |
| --- | --- | --- |
| Vòng điều khiển máy 1 | Xử lý xong rồi chờ thêm 200 ms | Thời gian xử lý được tính vào chu kỳ 200 ms |
| Đọc trạng thái relay định kỳ | Tối đa 4 lần gửi liên tiếp khi lỗi | Thử 1 lần, đánh dấu chưa xác nhận nếu lỗi và kiểm tra lại ở kỳ tiếp theo |
| Ghi relay và đọc xác nhận sau ghi | Dùng số lần thử trong YAML | Tiếp tục dùng số lần thử trong YAML |
| Dừng chương trình | Đóng MQTT trước khi tắt tải | Chờ vòng điều khiển kết thúc, yêu cầu tải OFF, đóng COM rồi đóng MQTT |
| SCADA tải lịch sử | Giữ cờ cập nhật trạng thái trong khi chờ lịch sử | Tải lịch sử độc lập; trạng thái tiếp tục cập nhật mỗi giây |
| REST ThingsBoard | Trạng thái, lịch sử và RPC dùng chung khóa HTTP | Mỗi loại có session và khóa riêng; một yêu cầu lịch sử/RPC chậm không giữ khóa HTTP của trạng thái |

Ví dụ một vòng xử lý mất 40 ms: trước đây khoảng cách giữa hai lần bắt đầu
đọc là khoảng 240 ms; hiện tại là khoảng 200 ms. Nếu một vòng mất hơn 200 ms,
vòng tiếp theo bắt đầu ngay khi có thể, không chạy bù hàng loạt các kỳ đã lỡ.
Mô phỏng dùng cùng cách tính khoảng chờ, còn thời gian mô hình nhiệt vẫn lấy
từ đồng hồ đơn điệu.

Với `timeout_ms: 1000`, `retries: 3`, `retry_delay_ms: 100`, ngân sách chờ
timeout cấu hình của một lần đọc giám sát relay trước đây là 4 × 1000 +
3 × 100 = 4300 ms. Hiện tại chỉ có một lần thử, tương ứng 1000 ms timeout
cấu hình. Đây **không phải giới hạn cứng của thời gian thực tế**: mở lại cổng,
nhận từng phần của frame, driver USB và lịch chạy của hệ điều hành có thể làm
giao dịch lâu hơn. Timeout của PT100 và của lệnh bật/tắt vẫn giữ nguyên.

Nhịp giám sát relay là tối thiểu một giây kể từ lần kiểm tra định kỳ trước
kết thúc, khi vòng điều khiển có thể thực hiện. Thất bại làm
`relay_connected=false`, phản hồi coil trên HMI/SCADA chuyển thành chưa xác
nhận. Lần thử ít hơn có thể làm trạng thái kết nối chập chờn dễ thấy hơn trên
bus đang lỗi; không dùng bit cũ để giả định lần kiểm tra đã thành công.

## SCADA

Trạng thái vẫn được trình duyệt hỏi mỗi giây; mỗi yêu cầu trạng thái có thời
gian chờ 5 giây. Lịch sử tự tải lại tối đa một lần mỗi 60 giây, kể cả sau lỗi,
với thời gian chờ 25 giây ở trình duyệt. Đổi khoảng lịch sử sẽ hủy yêu cầu cũ
ở trình duyệt và ưu tiên kết quả của khoảng vừa chọn. Việc hủy ở trình duyệt
không bảo đảm truy vấn backend/ThingsBoard đã dừng.

Phân tách session không loại bỏ thời gian đăng nhập/làm mới JWT: các yêu cầu
vẫn dùng chung khóa bảo vệ token. Đường truyền và ThingsBoard vẫn quyết định
một phần độ trễ RPC. Thời gian telemetry máy 1 gửi lên cloud vẫn là một giây.

## Giới hạn và xác minh

Chương trình không phải hệ điều khiển thời gian thực cứng. Lệnh HMI vẫn cần
chờ giao dịch đang giữ bus/controller kết thúc; dừng chương trình vẫn phải
gửi và xác nhận lệnh OFF. Các tối ưu này không sửa nguyên nhân PT100 ID 2
phát dữ liệu trong lúc máy đang chờ relay ID 1. Cần dùng phép thử nghe thụ
động để xác nhận tự gửi, rồi kiểm tra thời gian phản hồi và cấu hình thiết bị.

Các test bằng thiết bị giả kiểm tra chu kỳ có tính thời gian I/O, vòng quá
hạn không chạy bù, chính sách thử riêng cho giám sát không ảnh hưởng lệnh ghi,
và thứ tự tắt tải trước khi đóng cloud. Test SCADA giữ một yêu cầu lịch sử
hoặc RPC đang chờ và kiểm tra telemetry REST vẫn trả về độc lập.

```powershell
python -X utf8 -m unittest discover -s control_station/tests -p "test_*.py"
python -X utf8 -m unittest discover -s remote_scada/tests -p "test_*.py"
```

Kiểm tra phần cứng ở máy 1, sau khi đóng các script khác đang dùng COM5:

```powershell
python -m control_station.main --hardware
```

Khởi động lại SCADA ở máy 2 và dùng Ctrl+F5 để nạp JavaScript mới:

```powershell
python -m remote_scada.server
```

Test giả không xác nhận được thời gian phản hồi thực tế trên COM5 hoặc độ
trễ ThingsBoard. Cần đọc log `elapsed_ms`, `attempt` và đối chiếu trạng thái
HMI/SCADA trong lần chạy thật.
