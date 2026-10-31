"""Test AUTO thật bằng cùng runtime/controller của HMI; thời gian lấy từ YAML."""

import argparse
import math
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from control_station.configuration import DEFAULT_CONFIG_PATH, load_configuration
from control_station.models import (
    CommandSource,
    CommandType,
    ControlCommand,
    ControlMode,
    AutoPhase,
)
from control_station.runtime import StationRuntime


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument(
        "--drying-minutes",
        type=float,
        help="Thời gian giữ nhiệt đủ điều kiện; mặc định lấy YAML.",
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=float,
        default=1800,
        help="Hạn chờ toàn bộ mẻ, gồm gia nhiệt và nguội; hết hạn hủy mẻ.",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_wait_seconds) or args.max_wait_seconds <= 0:
        parser.error("Hạn chờ test phải hữu hạn và lớn hơn 0.")
    if args.drying_minutes is not None and (
        not math.isfinite(args.drying_minutes * 60) or args.drying_minutes <= 0
    ):
        parser.error("Thời gian sấy phải hữu hạn và lớn hơn 0 phút.")
    runtime = StationRuntime(load_configuration(args.config))
    result = 0
    try:
        runtime.poll_once()
        if args.drying_minutes is not None:
            duration = runtime.controller.process_command(
                ControlCommand(
                    CommandType.SET_DRYING_DURATION,
                    CommandSource.SYSTEM,
                    args.drying_minutes * 60,
                )
            )
            if not duration.accepted:
                raise RuntimeError(duration.message)
        accepted = runtime.controller.process_command(
            ControlCommand(CommandType.START_DRYING, CommandSource.SYSTEM)
        )
        if not accepted.accepted:
            raise RuntimeError(accepted.message)
        started = time.monotonic()
        cooling_notice_shown = False
        while time.monotonic() - started < args.max_wait_seconds:
            runtime.poll_once()
            state = runtime.controller.get_state_snapshot()
            print(
                f"{state.temperature_c} °C | {state.auto_phase.value} | còn {state.drying_remaining_s:.1f} s | đếm={state.hold_timer_running} | heater={state.heater_on}, process_fan={state.process_fan_on}",
                flush=True,
            )
            if state.mode == ControlMode.FAULT:
                raise RuntimeError(state.alarm_code.value)
            if state.auto_phase == AutoPhase.COOLING and not cooling_notice_shown:
                print("Hết thời gian sấy: mở cửa thoát ẩm thủ công để làm nguội.")
                cooling_notice_shown = True
            if state.auto_phase == AutoPhase.COMPLETE:
                print("Mẻ sấy hoàn tất, hai tải đã tắt.")
                break
            time.sleep(runtime.config.poll_interval_s)
        else:
            raise TimeoutError("Hết hạn chờ toàn bộ mẻ; hủy mẻ và thử tắt tải.")
    except KeyboardInterrupt:
        result = 130
    except Exception as error:
        print(f"Test AUTO thất bại: {error}", file=sys.stderr)
        result = 1
    finally:
        if not runtime.stop():
            result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
