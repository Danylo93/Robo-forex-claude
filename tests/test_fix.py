"""Testes da camada FIX: mensagens, sessão e adaptador de corretora."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from hft.config import HftSettings
from hft.fix import message as fix
from hft.fix.broker import FixBroker
from hft.fix.message import FixError, FixMessage, checksum, split_stream, utc_timestamp
from hft.fix.session import FixConfig, FixSession
from hft.fix.transport import LoopbackTransport
from hft.models import Side

SOH = fix.SOH


class Clock:
    """Relógio controlado pelos testes."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def raw(*pairs: tuple[int, str]) -> str:
    return "".join(f"{tag}={value}{SOH}" for tag, value in pairs)


def build_incoming(msg_type: str, seq: int, extra: str = "", sender="BROKER", target="CLIENT") -> bytes:
    """Monta uma mensagem vinda da contraparte, com BodyLength e CheckSum válidos."""
    body = raw((35, msg_type), (49, sender), (56, target), (34, str(seq)),
               (52, "20260730-10:00:00.000")) + extra
    prefix = f"8=FIX.4.4{SOH}9={len(body)}{SOH}"
    without = prefix + body
    return (without + f"10={checksum(without)}{SOH}").encode("ascii")


class MessageTests(unittest.TestCase):
    def test_encode_has_valid_length_and_checksum(self):
        message = FixMessage(fix.LOGON).set(fix.HEART_BT_INT, 30)
        payload = message.encode(sender="CLIENT", target="BROKER", seq_num=1,
                                 sending_time="20260730-10:00:00.000")
        decoded = FixMessage.decode(payload)  # decode valida 9 e 10
        self.assertEqual(decoded.msg_type, "A")
        self.assertEqual(decoded.get(fix.HEART_BT_INT), "30")
        self.assertEqual(decoded.get(fix.SENDER_COMP_ID), "CLIENT")

    def test_field_order_starts_with_8_9_35(self):
        payload = FixMessage(fix.HEARTBEAT).encode(sender="A", target="B", seq_num=7).decode()
        tags = [chunk.split("=")[0] for chunk in payload.split(SOH) if chunk]
        self.assertEqual(tags[:3], ["8", "9", "35"])
        self.assertEqual(tags[-1], "10")

    def test_checksum_mismatch_raises(self):
        payload = FixMessage(fix.HEARTBEAT).encode(sender="A", target="B", seq_num=1).decode()
        broken = payload[:-4] + "999" + SOH
        with self.assertRaises(FixError):
            FixMessage.decode(broken)

    def test_body_length_mismatch_raises(self):
        good = FixMessage(fix.HEARTBEAT).encode(sender="A", target="B", seq_num=1).decode()
        tampered = good.replace(f"{SOH}9=", f"{SOH}9=9", 1)
        with self.assertRaises(FixError):
            FixMessage.decode(tampered)

    def test_decode_requires_trailing_soh(self):
        with self.assertRaises(FixError):
            FixMessage.decode("8=FIX.4.4")

    def test_set_replaces_append_does_not(self):
        message = FixMessage("D")
        message.set(fix.SYMBOL, "EURUSD").set(fix.SYMBOL, "GBPUSD")
        self.assertEqual(message.get_all(fix.SYMBOL), ["GBPUSD"])
        message.append(fix.MD_ENTRY_TYPE, "0").append(fix.MD_ENTRY_TYPE, "1")
        self.assertEqual(message.get_all(fix.MD_ENTRY_TYPE), ["0", "1"])

    def test_groups_parses_repeating_block(self):
        message = FixMessage(fix.MARKET_DATA_SNAPSHOT)
        message.append(fix.NO_MD_ENTRIES, 3)
        for entry_type, price, size in (("0", "1.1000", "1000000"), ("0", "1.0999", "2000000"),
                                        ("1", "1.1002", "1500000")):
            message.append(fix.MD_ENTRY_TYPE, entry_type)
            message.append(fix.MD_ENTRY_PX, price)
            message.append(fix.MD_ENTRY_SIZE, size)
        entries = list(message.groups(fix.NO_MD_ENTRIES, fix.MD_ENTRY_TYPE))
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0][fix.MD_ENTRY_PX], "1.1000")
        self.assertEqual(entries[2][fix.MD_ENTRY_TYPE], "1")

    def test_split_stream_handles_partial_and_multiple(self):
        first = FixMessage(fix.HEARTBEAT).encode(sender="A", target="B", seq_num=1).decode()
        second = FixMessage(fix.TEST_REQUEST).encode(sender="A", target="B", seq_num=2).decode()
        messages, rest = split_stream(first + second[:10])
        self.assertEqual(len(messages), 1)
        self.assertTrue(rest.startswith("8=FIX.4.4"))
        messages2, rest2 = split_stream(rest + second[10:])
        self.assertEqual(len(messages2), 1)
        self.assertEqual(rest2, "")

    def test_split_stream_empty(self):
        self.assertEqual(split_stream(""), ([], ""))

    def test_utc_timestamp_format(self):
        stamp = utc_timestamp(datetime(2026, 7, 30, 10, 0, 0, 123_000, tzinfo=timezone.utc))
        self.assertEqual(stamp, "20260730-10:00:00.123")

    def test_get_helpers(self):
        message = FixMessage("8").set(fix.LAST_PX, "1.10501").set(fix.ORDER_QTY, "10000")
        self.assertAlmostEqual(message.get_float(fix.LAST_PX), 1.10501)
        self.assertEqual(message.get_int(fix.ORDER_QTY), 10000)
        self.assertEqual(message.get_int(999, 5), 5)
        self.assertTrue(message.has(fix.LAST_PX))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.transport = LoopbackTransport()
        self.config = FixConfig(
            sender_comp_id="CLIENT", target_comp_id="BROKER", heartbeat_interval=30
        )
        self.session = FixSession(self.config, self.transport, clock=self.clock)

    def sent(self) -> list[FixMessage]:
        return [FixMessage.decode(payload) for payload in self.transport.outbound]

    def test_connect_sends_logon(self):
        self.session.connect()
        logon = self.sent()[0]
        self.assertEqual(logon.msg_type, fix.LOGON)
        self.assertEqual(logon.get(fix.HEART_BT_INT), "30")
        self.assertEqual(logon.get(fix.RESET_SEQ_NUM_FLAG), "Y")
        self.assertEqual(logon.seq_num, 1)

    def test_logon_response_marks_session_up(self):
        self.session.connect()
        self.transport.feed(build_incoming(fix.LOGON, 1, raw((108, "30"))))
        self.assertEqual(self.session.poll(), [])
        self.assertTrue(self.session.state.logged_on)
        self.assertEqual(self.session.state.incoming_seq, 2)

    def test_application_message_is_returned(self):
        self.session.connect()
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.session.poll()
        self.transport.feed(build_incoming(fix.EXECUTION_REPORT, 2, raw((11, "O1"), (150, "F"))))
        messages = self.session.poll()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].msg_type, fix.EXECUTION_REPORT)

    def test_test_request_is_answered_with_heartbeat(self):
        self.session.connect()
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.session.poll()
        self.transport.feed(build_incoming(fix.TEST_REQUEST, 2, raw((112, "ABC"))))
        self.session.poll()
        last = self.sent()[-1]
        self.assertEqual(last.msg_type, fix.HEARTBEAT)
        self.assertEqual(last.get(fix.TEST_REQ_ID), "ABC")

    def test_heartbeat_is_sent_after_interval(self):
        self.session.connect()
        self.clock.advance(31)
        self.session.poll()
        self.assertEqual(self.sent()[-1].msg_type, fix.HEARTBEAT)

    def test_test_request_after_counterparty_silence(self):
        self.session.connect()
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.session.poll()
        self.clock.advance(40)
        self.session.poll()
        types = [m.msg_type for m in self.sent()]
        self.assertIn(fix.TEST_REQUEST, types)

    def test_sequence_gap_triggers_resend_request(self):
        self.session.connect()
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.session.poll()
        self.transport.feed(build_incoming(fix.EXECUTION_REPORT, 9))  # esperado 2
        messages = self.session.poll()
        self.assertEqual(messages, [])  # mensagem fora de ordem não é entregue
        resend = self.sent()[-1]
        self.assertEqual(resend.msg_type, fix.RESEND_REQUEST)
        self.assertEqual(resend.get(fix.BEGIN_SEQ_NO), "2")

    def test_resend_request_is_answered_with_gap_fill(self):
        self.session.connect()
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.session.poll()
        self.transport.feed(build_incoming(fix.RESEND_REQUEST, 2, raw((7, "1"), (16, "0"))))
        self.session.poll()
        reply = self.sent()[-1]
        self.assertEqual(reply.msg_type, fix.SEQUENCE_RESET)
        self.assertEqual(reply.get(fix.GAP_FILL_FLAG), "Y")

    def test_sequence_reset_adjusts_expectation(self):
        self.session.connect()
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.session.poll()
        self.transport.feed(build_incoming(fix.SEQUENCE_RESET, 99, raw((36, "50"))))
        self.session.poll()
        self.assertEqual(self.session.state.incoming_seq, 50)

    def test_logout_closes_session(self):
        self.session.connect()
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.session.poll()
        self.transport.feed(build_incoming(fix.LOGOUT, 2, raw((58, "tchau"))))
        self.session.poll()
        self.assertFalse(self.session.state.logged_on)
        self.assertEqual(self.sent()[-1].msg_type, fix.LOGOUT)

    def test_invalid_message_is_recorded_not_raised(self):
        self.session.connect()
        self.transport.feed(b"8=FIX.4.4\x019=10\x0135=0\x0110=000\x01")
        self.assertEqual(self.session.poll(), [])
        self.assertTrue(self.session.state.rejects)

    def test_disconnect_marks_session_down(self):
        class Broken(LoopbackTransport):
            def receive(self, max_bytes: int = 65536) -> bytes:
                raise ConnectionError("caiu")

        transport = Broken()
        session = FixSession(self.config, transport, clock=self.clock)
        session.connect()
        session.poll()
        self.assertFalse(session.state.logged_on)

    def test_outgoing_sequence_increments(self):
        self.session.connect()
        self.session.send(FixMessage(fix.HEARTBEAT))
        self.session.send(FixMessage(fix.HEARTBEAT))
        self.assertEqual([m.seq_num for m in self.sent()], [1, 2, 3])


class FixBrokerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.transport = LoopbackTransport()
        self.settings = HftSettings()
        self.broker = FixBroker(
            self.settings,
            FixConfig(sender_comp_id="CLIENT", target_comp_id="BROKER"),
            transport=self.transport,
            clock=self.clock,
        )

    def snapshot_message(self, seq: int = 2) -> bytes:
        entries = (
            raw((268, "4"))
            + raw((269, "0"), (270, "1.10000"), (271, "1000000"))
            + raw((269, "0"), (270, "1.09990"), (271, "2000000"))
            + raw((269, "1"), (270, "1.10012"), (271, "1500000"))
            + raw((269, "1"), (270, "1.10020"), (271, "3000000"))
        )
        return build_incoming(fix.MARKET_DATA_SNAPSHOT, seq, entries)

    def test_connect_logs_on_and_subscribes(self):
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.broker.connect(timeout=1.0)
        sent = [FixMessage.decode(p) for p in self.transport.outbound]
        self.assertEqual(sent[0].msg_type, fix.LOGON)
        request = sent[-1]
        self.assertEqual(request.msg_type, fix.MARKET_DATA_REQUEST)
        self.assertEqual(request.get(fix.SYMBOL), "EURUSD")
        self.assertEqual(request.get_all(fix.MD_ENTRY_TYPE), ["0", "1"])

    def test_snapshot_builds_book_and_tick(self):
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.broker.connect(timeout=1.0)
        self.transport.feed(self.snapshot_message())
        tick = self.broker.tick()
        self.assertIsNotNone(tick)
        self.assertAlmostEqual(tick.bid, 1.10000)
        self.assertAlmostEqual(tick.ask, 1.10012)
        self.assertEqual(len(self.broker.book.bids), 2)

    def test_incremental_updates_and_deletes(self):
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.broker.connect(timeout=1.0)
        self.transport.feed(self.snapshot_message())
        self.broker.tick()
        incremental = (
            raw((268, "2"))
            + raw((279, "1"), (269, "0"), (270, "1.10000"), (271, "500000"))
            + raw((279, "2"), (269, "1"), (270, "1.10012"), (271, "0"))
        )
        self.transport.feed(build_incoming(fix.MARKET_DATA_INCREMENTAL, 3, incremental))
        tick = self.broker.tick()
        self.assertAlmostEqual(tick.bid_size, 500000.0)
        self.assertAlmostEqual(tick.ask, 1.10020)  # nível apagado

    def test_order_is_sent_and_filled(self):
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.broker.connect(timeout=1.0)
        self.transport.feed(self.snapshot_message())
        tick = self.broker.tick()

        # responde a próxima ordem com um execution report preenchido
        original_send = self.broker.session.send

        def send_and_reply(message):
            payload = original_send(message)
            if message.msg_type == fix.NEW_ORDER_SINGLE:
                report = raw((11, message.get(fix.CL_ORD_ID)), (150, "F"), (39, "2"),
                             (31, "1.10013"), (32, "10000"))
                self.transport.feed(build_incoming(fix.EXECUTION_REPORT, 3, report))
            return payload

        self.broker.session.send = send_and_reply
        fill = self.broker.open(Side.BUY, 0.1, tick, 0.0, 0.0)
        self.assertAlmostEqual(fill.price, 1.10013)
        self.assertAlmostEqual(fill.lots, 0.1)
        order = [
            FixMessage.decode(p) for p in self.transport.outbound
            if FixMessage.decode(p).msg_type == fix.NEW_ORDER_SINGLE
        ][0]
        self.assertEqual(order.get(fix.SIDE), "1")
        self.assertEqual(order.get(fix.ORDER_QTY), "10000")
        self.assertEqual(order.get(fix.ORD_TYPE), "1")

    def test_rejected_order_raises(self):
        from hft.fix.broker import FixBrokerError

        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.broker.connect(timeout=1.0)
        self.transport.feed(self.snapshot_message())
        tick = self.broker.tick()
        original_send = self.broker.session.send

        def send_and_reject(message):
            payload = original_send(message)
            if message.msg_type == fix.NEW_ORDER_SINGLE:
                report = raw((11, message.get(fix.CL_ORD_ID)), (150, "8"), (39, "8"),
                             (58, "sem margem"))
                self.transport.feed(build_incoming(fix.EXECUTION_REPORT, 3, report))
            return payload

        self.broker.session.send = send_and_reject
        with self.assertRaises(FixBrokerError) as ctx:
            self.broker.open(Side.BUY, 0.1, tick, 0.0, 0.0)
        self.assertIn("sem margem", str(ctx.exception))

    def test_connect_timeout_without_logon(self):
        from hft.fix.broker import FixBrokerError

        with self.assertRaises(FixBrokerError):
            self.broker.connect(timeout=0.0)

    def test_shutdown_sends_logout(self):
        self.transport.feed(build_incoming(fix.LOGON, 1))
        self.broker.connect(timeout=1.0)
        self.broker.shutdown()
        types = [FixMessage.decode(p).msg_type for p in self.transport.outbound]
        self.assertIn(fix.LOGOUT, types)
        self.assertTrue(self.transport.closed)


if __name__ == "__main__":
    unittest.main()
