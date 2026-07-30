"""Camada de sessão FIX 4.4: logon, heartbeat, números de sequência e gaps.

A camada de sessão é onde integrações quebram. O que está implementado aqui é o
mínimo que um ECN exige para não derrubar a conexão:

- Logon com `ResetSeqNumFlag` e credenciais vindas de variável de ambiente;
- Heartbeat no intervalo negociado e `TestRequest` quando a contraparte silencia;
- validação do `MsgSeqNum` de entrada, com `ResendRequest` no primeiro gap;
- resposta a `ResendRequest` com `SequenceReset-GapFill` (não retransmitimos
  ordens antigas de propósito: reenviar ordem velha é pior que não reenviar);
- `Logout` limpo, com espera pela confirmação.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import message as fix
from .message import FixError, FixMessage
from .transport import Transport


@dataclass
class FixConfig:
    host: str = "127.0.0.1"
    port: int = 5001
    sender_comp_id: str = "CLIENT"
    target_comp_id: str = "BROKER"
    begin_string: str = "FIX.4.4"
    username: str = ""
    password_env: str = "FIX_PASSWORD"
    heartbeat_interval: int = 30
    reset_seq_on_logon: bool = True
    use_ssl: bool = True
    target_sub_id: str = ""


@dataclass
class SessionState:
    outgoing_seq: int = 1
    incoming_seq: int = 1
    logged_on: bool = False
    last_sent: float = 0.0
    last_received: float = 0.0
    test_request_sent: bool = False
    resend_requested_upto: int = 0
    logout_sent: bool = False
    rejects: list[str] = field(default_factory=list)


class FixSession:
    """Sessão FIX orientada a polling — combina com o laço do motor."""

    def __init__(
        self,
        config: FixConfig,
        transport: Transport,
        clock: Callable[[], float] = time.monotonic,
        on_event: Optional[Callable[[str, dict], None]] = None,
    ):
        self.config = config
        self.transport = transport
        self.clock = clock
        self.state = SessionState()
        self.on_event = on_event
        self._buffer = ""

    # ------------------------------------------------------------- conexão
    def connect(self) -> None:
        self.transport.connect()
        now = self.clock()
        self.state.last_sent = now
        self.state.last_received = now
        self.send(self._logon_message())

    def _logon_message(self) -> FixMessage:
        cfg = self.config
        message = FixMessage(fix.LOGON)
        message.set(fix.ENCRYPT_METHOD, 0)
        message.set(fix.HEART_BT_INT, cfg.heartbeat_interval)
        if cfg.reset_seq_on_logon:
            message.set(fix.RESET_SEQ_NUM_FLAG, "Y")
            self.state.outgoing_seq = 1
            self.state.incoming_seq = 1
        if cfg.username:
            message.set(fix.USERNAME, cfg.username)
            password = os.environ.get(cfg.password_env, "")
            if password:
                message.set(fix.PASSWORD, password)
        return message

    def logout(self, text: str = "encerrando") -> None:
        if not self.transport.connected or self.state.logout_sent:
            return
        message = FixMessage(fix.LOGOUT)
        message.set(fix.TEXT, text)
        self.send(message)
        self.state.logout_sent = True

    def close(self) -> None:
        self.transport.close()
        self.state.logged_on = False

    # --------------------------------------------------------------- envio
    def send(self, message: FixMessage) -> bytes:
        cfg = self.config
        if cfg.target_sub_id and not message.has(57):
            message.set(57, cfg.target_sub_id)
        payload = message.encode(
            begin_string=cfg.begin_string,
            sender=cfg.sender_comp_id,
            target=cfg.target_comp_id,
            seq_num=self.state.outgoing_seq,
        )
        self.transport.send(payload)
        self.state.outgoing_seq += 1
        self.state.last_sent = self.clock()
        self._emit("enviado", {"tipo": message.msg_type, "seq": self.state.outgoing_seq - 1})
        return payload

    # ------------------------------------------------------------- recepção
    def poll(self) -> list[FixMessage]:
        """Lê o socket, trata as mensagens de sessão e devolve as de aplicação."""
        application: list[FixMessage] = []
        try:
            data = self.transport.receive()
        except ConnectionError as exc:
            self.state.logged_on = False
            self._emit("desconectado", {"motivo": str(exc)})
            return application
        if data:
            self._buffer += data.decode("ascii", errors="replace")
            raw_messages, self._buffer = fix.split_stream(self._buffer)
            for raw in raw_messages:
                try:
                    message = FixMessage.decode(raw)
                except FixError as exc:
                    self.state.rejects.append(str(exc))
                    self._emit("invalida", {"erro": str(exc)})
                    continue
                self.state.last_received = self.clock()
                handled = self._handle_session(message)
                if not handled:
                    application.append(message)
        self._keepalive()
        return application

    def _handle_session(self, message: FixMessage) -> bool:
        """Trata mensagens da camada de sessão. True = consumida aqui."""
        msg_type = message.msg_type

        # SequenceReset-Reset ajusta a expectativa antes de qualquer validação
        if msg_type == fix.SEQUENCE_RESET:
            new_seq = message.get_int(fix.NEW_SEQ_NO, self.state.incoming_seq)
            self.state.incoming_seq = max(new_seq, 1)
            self.state.resend_requested_upto = 0
            self._emit("sequence_reset", {"novo": self.state.incoming_seq})
            return True

        if not self._check_sequence(message):
            return True

        if msg_type == fix.LOGON:
            self.state.logged_on = True
            negotiated = message.get_int(fix.HEART_BT_INT, self.config.heartbeat_interval)
            self.config.heartbeat_interval = negotiated or self.config.heartbeat_interval
            self._emit("logon", {"heartbeat": self.config.heartbeat_interval})
            return True
        if msg_type == fix.HEARTBEAT:
            self.state.test_request_sent = False
            return True
        if msg_type == fix.TEST_REQUEST:
            reply = FixMessage(fix.HEARTBEAT)
            reply.set(fix.TEST_REQ_ID, message.get(fix.TEST_REQ_ID, ""))
            self.send(reply)
            return True
        if msg_type == fix.RESEND_REQUEST:
            self._gap_fill(message)
            return True
        if msg_type == fix.LOGOUT:
            self.state.logged_on = False
            if not self.state.logout_sent:
                self.logout("respondendo logout")
            self._emit("logout", {"texto": message.get(fix.TEXT, "")})
            return True
        if msg_type == fix.REJECT:
            self.state.rejects.append(message.get(fix.TEXT, "rejeitada"))
            self._emit("reject", {"texto": message.get(fix.TEXT, ""), "ref": message.get(45, "")})
            return True
        return False

    def _check_sequence(self, message: FixMessage) -> bool:
        """Valida o número de sequência; pede reenvio no primeiro gap."""
        expected = self.state.incoming_seq
        received = message.seq_num
        if received == expected:
            self.state.incoming_seq += 1
            self.state.resend_requested_upto = 0
            return True
        if received < expected:
            # duplicata: aceitável quando marcada como PossDup
            if message.get(fix.POSS_DUP_FLAG) == "Y":
                return False
            self._emit("sequencia_baixa", {"recebido": received, "esperado": expected})
            return False
        # gap: pede o que faltou uma única vez
        if self.state.resend_requested_upto < received:
            request = FixMessage(fix.RESEND_REQUEST)
            request.set(fix.BEGIN_SEQ_NO, expected)
            request.set(fix.END_SEQ_NO, 0)  # 0 = até a última
            self.send(request)
            self.state.resend_requested_upto = received
            self._emit("gap", {"esperado": expected, "recebido": received})
        return False

    def _gap_fill(self, request: FixMessage) -> None:
        """Responde a ResendRequest com GapFill até a sequência atual."""
        reply = FixMessage(fix.SEQUENCE_RESET)
        reply.set(fix.GAP_FILL_FLAG, "Y")
        reply.set(fix.NEW_SEQ_NO, self.state.outgoing_seq + 1)
        reply.set(fix.POSS_DUP_FLAG, "Y")
        begin = request.get_int(fix.BEGIN_SEQ_NO, self.state.outgoing_seq)
        saved = self.state.outgoing_seq
        self.state.outgoing_seq = max(begin, 1)
        self.send(reply)
        self.state.outgoing_seq = saved + 1
        self._emit("gap_fill", {"ate": self.state.outgoing_seq})

    def _keepalive(self) -> None:
        """Heartbeat no intervalo e TestRequest quando a contraparte cala."""
        if not self.transport.connected:
            return
        interval = max(self.config.heartbeat_interval, 1)
        now = self.clock()
        if now - self.state.last_sent >= interval:
            self.send(FixMessage(fix.HEARTBEAT))
        silence = now - self.state.last_received
        if silence >= interval * 1.2 and not self.state.test_request_sent:
            request = FixMessage(fix.TEST_REQUEST)
            request.set(fix.TEST_REQ_ID, f"TR{int(now * 1000) % 10**9}")
            self.send(request)
            self.state.test_request_sent = True
        elif silence >= interval * 2.4 and self.state.test_request_sent:
            self._emit("sem_resposta", {"silencio_s": round(silence, 1)})
            self.state.logged_on = False

    def _emit(self, event: str, data: dict) -> None:
        if self.on_event:
            self.on_event(event, data)
