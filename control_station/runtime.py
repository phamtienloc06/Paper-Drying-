"""Vòng đọc PT100–controller dùng một bus duy nhất, khởi động OFF và giữ FAULT khi reconnect."""

import logging
import threading
import time

from .configuration import StationConfiguration
from .controller import SystemController
from .modbus import ModbusCommunicationError, ModbusRTU
from .models import SystemState
from .pt100 import PT100DataError, PT100Sensor
from .relay import RelayModule

logger = logging.getLogger(__name__)


class StationRuntime:
    def __init__(self, config: StationConfiguration, *, bus=None, clock=None) -> None:
        """Tạo cảm biến, relay, controller và các trạng thái của vòng runtime."""
        self.config = config
        self.bus = bus if bus is not None else ModbusRTU(config.modbus)
        self.sensor = PT100Sensor(self.bus, config.pt100)
        self.relay = RelayModule(self.bus, config.relay)
        self.controller = SystemController(
            SystemState(), config.control, self.relay, clock=clock
        )
        self._connected = False
        self._stop_event = threading.Event()
        self._thread = None
        self._clock = clock or time.monotonic
        self._last_relay_check: float | None = None
        self.mqtt_client = None

    def attach_mqtt(self, mqtt_client) -> None:
        """Gắn cầu nối telemetry/RPC tùy chọn mà không ghép chặt vào I/O cục bộ."""
        if self.mqtt_client is not None:
            raise RuntimeError("MQTT client is already attached.")
        try:
            mqtt_client.start()
        except Exception:
            logger.exception("Could not start optional cloud bridge; local control continues.")
            return
        self.mqtt_client = mqtt_client

    def poll_once(self) -> None:
        """Không đọc thành công thì không dùng mẫu cũ để tiếp tục gia nhiệt."""
        try:
            if not self._connected:
                self.bus.connect()
                self._connected = True
            temperature = self.sensor.read_temperature()
        except ModbusCommunicationError as error:
            logger.warning("Mất giao tiếp thiết bị: %s", error)
            self._disconnect()
            self.controller.update_measurement(None, False, False)
        except PT100DataError as error:
            logger.warning("Dữ liệu PT100 không hợp lệ: %s", error)
            self.controller.update_measurement(None, False, True)
        else:
            self.controller.update_measurement(temperature, True, True)
        if self._stop_event.is_set():
            return
        if self.mqtt_client is not None:
            try:
                # Xử lý RPC trong vòng điều khiển để controller tại máy 1 quyết định lệnh.
                self.controller.update_mqtt_connection(self.mqtt_client.connected)
                self.mqtt_client.process_pending_commands()
            except Exception:
                logger.exception("Cloud command bridge failed; local control continues.")
        else:
            self.controller.update_mqtt_connection(False)
        self.controller.run_control_cycle()
        if self.mqtt_client is not None:
            try:
                self.mqtt_client.observe_alarm()
            except Exception:
                logger.exception("Could not capture cloud alarm event.")
        # Đọc trạng thái module cho HMI mỗi giây, trong cùng thread/bus.
        # Đây là giám sát kết nối; không đổi chính sách FAULT hay lệnh tải.
        now = self._clock()
        if not self._stop_event.is_set() and self._connected and (
            self._last_relay_check is None or now - self._last_relay_check >= 1.0
        ):
            try:
                # Giám sát định kỳ thử một lần; lần sau tự kiểm tra lại.
                # Đọc xác nhận sau lệnh bật/tắt vẫn dùng đầy đủ retries của bus.
                self.relay.read_states(retries=0)
            except Exception as error:
                logger.warning("Chưa xác nhận được phản hồi relay: %s", error)
            finally:
                self._last_relay_check = self._clock()

    def start(self) -> None:
        """Chạy một chu kỳ an toàn rồi khởi động luồng điều khiển định kỳ."""
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Runtime đã chạy.")
        self._stop_event.clear()
        self.poll_once()
        self._thread = threading.Thread(
            target=self._run, name="station-control-loop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        """Lặp chu kỳ điều khiển cho đến khi nhận yêu cầu dừng."""
        wait_s = self.config.poll_interval_s
        while not self._stop_event.wait(wait_s):
            started_at = self._clock()
            try:
                self.poll_once()
            except Exception:
                logger.exception("Vòng đọc–điều khiển thất bại.")
                self._disconnect()
                self.controller.update_measurement(None, False, False)
                self.controller.run_control_cycle()
            # Chu kỳ tính từ lúc bắt đầu đọc, không cộng thêm toàn bộ thời gian I/O.
            # Nếu quá hạn, đọc mẫu mới ngay; không chạy bù những chu kỳ đã lỡ.
            wait_s = max(0.0, self.config.poll_interval_s - (self._clock() - started_at))

    def stop(self) -> bool:
        """Chờ giao dịch đang chạy kết thúc trước khi tắt tải và đóng bus."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join()
        try:
            confirmed = self.controller.shutdown()
            if not confirmed:
                logger.error(
                    "Không xác nhận được tải OFF khi dừng; cần ngắt nguồn tải."
                )
            return confirmed
        finally:
            self._disconnect()
            # Đóng cloud sau khi đã yêu cầu tải OFF và giải phóng cổng COM.
            if self.mqtt_client is not None:
                try:
                    self.mqtt_client.stop()
                except Exception:
                    logger.exception("Could not stop cloud bridge cleanly.")
            self.controller.update_mqtt_connection(False)

    def _disconnect(self) -> None:
        """Đánh dấu bus mất kết nối và thử đóng kết nối thiết bị."""
        self._connected = False
        self._last_relay_check = None
        self.relay.mark_disconnected()
        try:
            self.bus.close()
        except Exception as error:
            logger.warning("Đóng bus thất bại: %s", error)
