import pytest

from fake_serial import FakeSerial
from lebox.bus import BusError
from lebox.feetech import FeetechBus, build_packet, checksum


def connect(servo_ids=(1,), **kwargs):
    fake = FakeSerial(servo_ids)
    bus = FeetechBus("/dev/fake", port_factory=lambda port, baud, timeout: fake, sleep=lambda dt: None, **kwargs)
    return bus, fake


def test_packets_match_the_documented_framing():
    # Read two bytes at address 56 (present position) from servo 1.
    assert build_packet(1, 0x02, bytes([56, 2])) == bytes([0xFF, 0xFF, 0x01, 0x04, 0x02, 0x38, 0x02, 0xBE])
    # Ping servo 1: FF FF 01 02 01 FB.
    assert build_packet(1, 0x01) == bytes([0xFF, 0xFF, 0x01, 0x02, 0x01, 0xFB])
    assert checksum(bytes([0x01, 0x02, 0x01])) == 0xFB


def test_ping_finds_servos_that_are_there():
    bus, _ = connect((1, 4))
    assert bus.ping(1) and bus.ping(4)
    assert not bus.ping(2)


def test_read_position_decodes_low_byte_first():
    bus, fake = connect()
    fake.set(1, 56, 3000, size=2)
    assert bus.read_position(1) == 3000
    assert fake.written[-1] == build_packet(1, 0x02, bytes([56, 2]))


def test_read_position_decodes_the_sign_bit():
    bus, fake = connect()
    fake.set(1, 56, 0x8000 | 12, size=2)
    assert bus.read_position(1) == -12


def test_torque_goal_and_limit_go_to_the_right_registers():
    bus, fake = connect()
    bus.set_torque(1, True)
    assert fake.get(1, 40) == 1
    bus.write_goal(1, 2500)
    assert fake.get(1, 42, size=2) == 2500
    bus.write_torque_limit(1, 150)
    assert fake.get(1, 48, size=2) == 150 and bus.read_torque_limit(1) == 150
    bus.set_torque(1, False)
    assert fake.get(1, 40) == 0


@pytest.mark.parametrize("goal", [-1, 4096, 100000])
def test_goals_outside_one_turn_are_refused_before_anything_is_sent(goal):
    bus, fake = connect()
    with pytest.raises(BusError, match="outside 0..4095"):
        bus.write_goal(1, goal)
    assert fake.written == []


@pytest.mark.parametrize("value", [-1, 1001, 150.0, True])
def test_torque_values_must_be_whole_per_mille(value):
    bus, fake = connect()
    with pytest.raises(BusError, match="0 to 1000"):
        bus.write_torque_limit(1, value)
    with pytest.raises(BusError, match="0 to 1000"):
        bus.write_torque_cap(1, value)
    assert fake.written == []


def test_torque_cap_is_stored_unlocking_and_relocking_the_servo():
    bus, fake = connect()
    bus.write_torque_cap(1, 300)
    assert fake.get(1, 16, size=2) == 300 and bus.read_torque_cap(1) == 300
    assert fake.get(1, 55) == 1  # locked again
    assert fake.lock_violations == 0
    # After a power cycle the working limit starts from the stored cap.
    bus.write_torque_limit(1, 900)
    fake.power_cycle()
    assert bus.read_torque_limit(1) == 300


def test_torque_cap_write_is_verified_by_reading_back():
    bus, fake = connect()
    original = fake._handle

    def stuck_lock(servo_id, instruction, params):
        if instruction == 0x03 and params[0] == 55:
            fake._reply(servo_id)  # acknowledges but stays locked
            return
        original(servo_id, instruction, params)

    fake._handle = stuck_lock
    with pytest.raises(BusError, match="did not take the torque cap"):
        bus.write_torque_cap(1, 300)


@pytest.mark.parametrize("reply_from_new_id", [False, True])
def test_write_id_moves_the_servo_and_locks_it_again(reply_from_new_id):
    bus, fake = connect((1,))
    fake.reply_to_id_write_from_new_id = reply_from_new_id
    bus.write_id(1, 3)
    assert not bus.ping(1) and bus.ping(3)
    assert fake.get(3, 5) == 3 and fake.get(3, 55) == 1


def test_write_id_refuses_an_id_that_is_taken_or_invalid():
    bus, fake = connect((1, 2))
    with pytest.raises(BusError, match="already taken"):
        bus.write_id(1, 2)
    with pytest.raises(BusError, match="must be 0..253"):
        bus.write_id(1, 254)
    assert fake.get(1, 5) == 1 and fake.get(1, 55) == 1
    bus.write_id(1, 1)  # no change is fine


def test_read_status_gathers_the_diagnostics():
    bus, fake = connect()
    fake.set(1, 62, 51)
    fake.set(1, 63, 31)
    status = bus.read_status(1)
    assert (status.model, status.position, status.voltage, status.temperature) == (777, 2048, 5.1, 31)
    assert (status.torque_enabled, status.torque_limit, status.torque_cap) == (False, 1000, 1000)


def test_a_missing_servo_is_reported_with_the_port():
    bus, _ = connect()
    with pytest.raises(BusError, match="no servo with id 9 answered on /dev/fake"):
        bus.read_position(9)


def test_lost_and_corrupt_replies_are_retried():
    bus, fake = connect()
    fake.drop_replies = 1
    assert bus.read_position(1) == 2048
    fake.corrupt_replies = 2
    assert bus.read_position(1) == 2048
    fake.corrupt_replies = 3  # more failures than retries
    with pytest.raises(BusError, match="checksum mismatch"):
        bus.read_position(1)


def test_noise_before_a_reply_is_skipped():
    bus, fake = connect()
    fake.noise_before_reply = b"\x00\x13\xff\x07"
    assert bus.read_position(1) == 2048


def test_a_reply_from_the_wrong_servo_is_rejected():
    bus, fake = connect((1, 2))
    original = fake._reply
    fake._reply = lambda servo_id, params=b"": original(2, params)
    with pytest.raises(BusError, match="reply came from id 2"):
        bus.read_position(1)


def test_closed_bus():
    bus, fake = connect()
    bus.close()
    assert not fake.is_open
    with pytest.raises(BusError, match="closed"):
        bus.read_position(1)
    with pytest.raises(BusError, match="closed"):
        bus.ping(1)
    bus.close()  # closing twice is harmless
