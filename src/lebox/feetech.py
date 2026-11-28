"""Driver for Feetech STS bus servos (STS3215) on a serial port.

It speaks the Feetech serial protocol directly through pyserial, so it does
not depend on any particular LeRobot version. The servo adapter is a plain
USB-to-serial bridge at 1 Mbit/s.

Packets (the same framing as Dynamixel protocol 1.0)::

    to servo    FF FF  id  length  instruction  parameters...  checksum
    from servo  FF FF  id  length  status       parameters...  checksum

``length`` counts everything after itself. ``checksum`` is the inverted low
byte of the sum of everything between the header and the checksum. Two-byte
values travel low byte first.

Register addresses follow the Feetech STS memory table. Addresses below 40
live in the servo's permanent memory and only take a write while the lock
register is 0.

This driver has been tested against an emulated servo only. Follow
``docs/bench-day.md`` for the first run on real hardware: read-only checks
come first.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .bus import BusError, ServoStatus, _permille

HEADER = b"\xff\xff"
PING, READ, WRITE = 0x01, 0x02, 0x03

# name: (address, size in bytes)
MODEL = (3, 2)
ID = (5, 1)
MAX_TORQUE = (16, 2)
TORQUE_ENABLE = (40, 1)
GOAL_POSITION = (42, 2)
TORQUE_LIMIT = (48, 2)
LOCK = (55, 1)
PRESENT_POSITION = (56, 2)
PRESENT_VOLTAGE = (62, 1)
PRESENT_TEMPERATURE = (63, 1)

FIRST_VOLATILE_ADDRESS = 40
MAX_ID = 253


def checksum(body: bytes) -> int:
    return ~sum(body) & 0xFF


def build_packet(servo_id: int, instruction: int, params: bytes = b"") -> bytes:
    body = bytes([servo_id, len(params) + 2, instruction]) + params
    return HEADER + body + bytes([checksum(body)])


def default_port_factory(port: str, baudrate: int, timeout: float) -> Any:
    try:
        import serial
    except ImportError:
        raise BusError("pyserial is not installed. Run: pip install pyserial") from None
    try:
        return serial.Serial(port, baudrate=baudrate, timeout=timeout)
    except (serial.SerialException, OSError) as exc:
        hint = ""
        if "ermission" in str(exc):
            hint = " On Ubuntu: sudo usermod -aG dialout $USER, then log out and back in."
        raise BusError(f"cannot open {port}: {exc}.{hint}") from None


def list_ports() -> list[tuple[str, str]]:
    """Serial ports on this computer as (device, description)."""
    try:
        from serial.tools import list_ports as tool
    except ImportError:
        raise BusError("pyserial is not installed. Run: pip install pyserial") from None
    return [(port.device, port.description) for port in sorted(tool.comports(), key=lambda port: port.device)]


class FeetechBus:
    def __init__(
        self,
        port: str,
        baudrate: int = 1_000_000,
        timeout: float = 0.05,
        retries: int = 2,
        port_factory: Callable[[str, int, float], Any] = default_port_factory,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.port = port
        self._retries = retries
        self._sleep = sleep
        self._serial = port_factory(port, baudrate, timeout)
        self.status_flags = 0
        """The status byte of the last reply; non-zero bits are servo alarms."""

    # Packets

    def _read_exact(self, count: int) -> bytes:
        data = self._serial.read(count)
        if len(data) != count:
            raise BusError("timed out")
        return data

    def _read_reply(self, expected_ids: tuple[int, ...]) -> bytes:
        # Skip anything before the header, without waiting forever on a noisy line.
        window = b""
        for _ in range(64):
            window = (window + self._read_exact(1))[-2:]
            if window == HEADER:
                break
        else:
            raise BusError("no packet header in the reply")
        servo_id, length = self._read_exact(2)
        if length < 2:
            raise BusError("malformed reply")
        rest = self._read_exact(length)
        body = bytes([servo_id, length]) + rest[:-1]
        if checksum(body) != rest[-1]:
            raise BusError("checksum mismatch in the reply")
        if servo_id not in expected_ids:
            raise BusError(f"reply came from id {servo_id}")
        self.status_flags = rest[0]
        return rest[1:-1]

    def _transact(
        self, servo_id: int, instruction: int, params: bytes = b"", reply_ids: tuple[int, ...] | None = None
    ) -> bytes:
        if self._serial is None:
            raise BusError("the bus is closed")
        packet = build_packet(servo_id, instruction, params)
        problem = "no attempt made"
        for _ in range(self._retries + 1):
            self._serial.reset_input_buffer()
            self._serial.write(packet)
            try:
                return self._read_reply(reply_ids or (servo_id,))
            except BusError as exc:
                problem = str(exc)
        raise BusError(f"no servo with id {servo_id} answered on {self.port} ({problem})")

    def _read(self, servo_id: int, register: tuple[int, int]) -> int:
        address, size = register
        data = self._transact(servo_id, READ, bytes([address, size]))
        if len(data) != size:
            raise BusError(f"servo {servo_id} sent {len(data)} byte(s) for a {size}-byte register")
        return int.from_bytes(data, "little")

    def _write(self, servo_id: int, register: tuple[int, int], value: int) -> None:
        address, size = register
        self._transact(servo_id, WRITE, bytes([address]) + value.to_bytes(size, "little"))

    def _write_stored(self, servo_id: int, register: tuple[int, int], value: int) -> None:
        """Write a register in the servo's permanent memory: unlock, write, lock again."""
        self._write(servo_id, LOCK, 0)
        try:
            self._write(servo_id, register, value)
            self._sleep(0.02)
        finally:
            self._write(servo_id, LOCK, 1)

    # The ServoBus interface

    def ping(self, servo_id: int) -> bool:
        if self._serial is None:
            raise BusError("the bus is closed")
        try:
            self._transact(servo_id, PING)
        except BusError:
            return False
        return True

    def read_position(self, servo_id: int) -> int:
        value = self._read(servo_id, PRESENT_POSITION)
        magnitude = value & 0x7FFF
        return -magnitude if value & 0x8000 else magnitude

    def set_torque(self, servo_id: int, enabled: bool) -> None:
        self._write(servo_id, TORQUE_ENABLE, 1 if enabled else 0)

    def write_goal(self, servo_id: int, raw: int) -> None:
        raw = int(raw)
        if not 0 <= raw <= 4095:
            raise BusError(f"goal position {raw} is outside 0..4095")
        self._write(servo_id, GOAL_POSITION, raw)

    def read_torque_limit(self, servo_id: int) -> int:
        return self._read(servo_id, TORQUE_LIMIT)

    def write_torque_limit(self, servo_id: int, permille: int) -> None:
        self._write(servo_id, TORQUE_LIMIT, _permille(permille))

    def read_torque_cap(self, servo_id: int) -> int:
        return self._read(servo_id, MAX_TORQUE)

    def write_torque_cap(self, servo_id: int, permille: int) -> None:
        self._write_stored(servo_id, MAX_TORQUE, _permille(permille))
        stored = self.read_torque_cap(servo_id)
        if stored != permille:
            raise BusError(f"servo {servo_id} did not take the torque cap: wrote {permille}, read back {stored}")

    def write_id(self, servo_id: int, new_id: int) -> None:
        if not 0 <= new_id <= MAX_ID:
            raise BusError(f"servo id must be 0..{MAX_ID}, got {new_id}")
        if new_id == servo_id:
            return
        if self.ping(new_id):
            raise BusError(f"id {new_id} is already taken on this bus")
        self._write(servo_id, LOCK, 0)
        try:
            # The reply to this write may come under the old or the new id.
            self._transact(servo_id, WRITE, bytes([ID[0], new_id]), reply_ids=(servo_id, new_id))
        except BusError:
            pass
        self._sleep(0.02)
        if not self.ping(new_id):
            raise BusError(f"servo {servo_id} did not take the new id {new_id}")
        self._write(new_id, LOCK, 1)

    def read_status(self, servo_id: int) -> ServoStatus:
        return ServoStatus(
            servo_id=servo_id,
            model=self._read(servo_id, MODEL),
            position=self.read_position(servo_id),
            voltage=self._read(servo_id, PRESENT_VOLTAGE) / 10.0,
            temperature=self._read(servo_id, PRESENT_TEMPERATURE),
            torque_enabled=bool(self._read(servo_id, TORQUE_ENABLE)),
            torque_limit=self.read_torque_limit(servo_id),
            torque_cap=self.read_torque_cap(servo_id),
        )

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None
