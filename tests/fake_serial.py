"""An emulated Feetech bus at byte level, standing in for pyserial in tests."""

from __future__ import annotations


def checksum(body: bytes) -> int:
    return ~sum(body) & 0xFF


def new_memory(servo_id: int) -> bytearray:
    memory = bytearray(80)
    memory[3:5] = (777).to_bytes(2, "little")  # model: STS3215
    memory[5] = servo_id
    memory[16:18] = (1000).to_bytes(2, "little")  # stored torque cap
    memory[48:50] = (1000).to_bytes(2, "little")  # working torque limit
    memory[55] = 1  # locked
    memory[56:58] = (2048).to_bytes(2, "little")  # position
    memory[62] = 50  # 5.0 V
    memory[63] = 25
    return memory


class FakeSerial:
    def __init__(self, servo_ids=(1,)):
        self.servos = {servo_id: new_memory(servo_id) for servo_id in servo_ids}
        self._incoming = b""
        self._outgoing = b""
        self.written = []
        self.is_open = True
        self.drop_replies = 0
        self.corrupt_replies = 0
        self.noise_before_reply = b""
        self.reply_to_id_write_from_new_id = False
        self.lock_violations = 0
        self.motors = False
        """When true, a servo with torque on jumps straight to its goal."""

    # pyserial's interface

    def write(self, data):
        self.written.append(bytes(data))
        self._incoming += bytes(data)
        self._process()
        return len(data)

    def read(self, count=1):
        data, self._outgoing = self._outgoing[:count], self._outgoing[count:]
        return data

    def reset_input_buffer(self):
        self._outgoing = b""

    def close(self):
        self.is_open = False

    # The emulated servos

    def set(self, servo_id, address, value, size=1):
        self.servos[servo_id][address : address + size] = value.to_bytes(size, "little")

    def get(self, servo_id, address, size=1):
        return int.from_bytes(self.servos[servo_id][address : address + size], "little")

    def power_cycle(self):
        for memory in self.servos.values():
            memory[40] = 0
            memory[48:50] = memory[16:18]
            memory[55] = 1

    def _process(self):
        while len(self._incoming) >= 6:
            if self._incoming[:2] != b"\xff\xff":
                self._incoming = self._incoming[1:]
                continue
            servo_id, length = self._incoming[2], self._incoming[3]
            if len(self._incoming) < 4 + length:
                return
            packet, self._incoming = self._incoming[: 4 + length], self._incoming[4 + length :]
            body = packet[2:-1]
            if checksum(body) != packet[-1]:
                continue
            self._handle(servo_id, packet[4], packet[5:-1])

    def _reply(self, servo_id, params=b""):
        if self.drop_replies:
            self.drop_replies -= 1
            return
        body = bytes([servo_id, len(params) + 2, 0]) + params
        packet = b"\xff\xff" + body + bytes([checksum(body)])
        if self.corrupt_replies:
            self.corrupt_replies -= 1
            packet = packet[:-1] + bytes([packet[-1] ^ 0x55])
        self._outgoing += self.noise_before_reply + packet

    def _handle(self, servo_id, instruction, params):
        memory = self.servos.get(servo_id)
        if memory is None:
            return  # nobody home: the caller times out
        if instruction == 0x01:
            self._reply(servo_id)
        elif instruction == 0x02:
            address, size = params
            self._reply(servo_id, bytes(memory[address : address + size]))
        elif instruction == 0x03:
            address, data = params[0], params[1:]
            reply_id = servo_id
            if address < 40 and memory[55] == 1:
                self.lock_violations += 1  # ignored, as on the real servo
            else:
                memory[address : address + len(data)] = data
                if self.motors and memory[40] == 1 and address in (40, 42):
                    memory[56:58] = memory[42:44]  # an ideal motor: it is at its goal at once
                if address == 5:
                    new_id = data[0]
                    self.servos[new_id] = self.servos.pop(servo_id)
                    if self.reply_to_id_write_from_new_id:
                        reply_id = new_id
            self._reply(reply_id)
