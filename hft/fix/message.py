"""Codificação e decodificação de mensagens FIX 4.4.

FIX é `tag=valor` separado por SOH (0x01). Duas regras dão trabalho e são a
fonte da maioria dos erros de integração:

- **BodyLength (9)**: número de bytes entre o SOH que fecha o campo 9 e o SOH
  que antecede o campo 10 (CheckSum).
- **CheckSum (10)**: soma de todos os bytes até (e incluindo) aquele mesmo SOH,
  módulo 256, com três dígitos.

A ordem dos campos importa: 8, 9, 35 vêm primeiro; 10 vem por último. Por isso a
mensagem guarda uma lista ordenada de pares, não um dicionário — grupos
repetitivos (mesma tag várias vezes) também dependem disso.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

SOH = "\x01"

# Tags usadas pelo projeto (nome -> número)
BEGIN_STRING = 8
BODY_LENGTH = 9
MSG_TYPE = 35
SENDER_COMP_ID = 49
TARGET_COMP_ID = 56
MSG_SEQ_NUM = 34
SENDING_TIME = 52
CHECKSUM = 10
POSS_DUP_FLAG = 43
ORIG_SENDING_TIME = 122

# Session
HEART_BT_INT = 108
ENCRYPT_METHOD = 98
USERNAME = 553
PASSWORD = 554
RESET_SEQ_NUM_FLAG = 141
TEST_REQ_ID = 112
GAP_FILL_FLAG = 123
NEW_SEQ_NO = 36
REF_SEQ_NUM = 45
TEXT = 58
BEGIN_SEQ_NO = 7
END_SEQ_NO = 16

# Aplicação
CL_ORD_ID = 11
ORIG_CL_ORD_ID = 41
ORDER_ID = 37
EXEC_ID = 17
EXEC_TYPE = 150
ORD_STATUS = 39
SYMBOL = 55
SIDE = 54
ORDER_QTY = 38
ORD_TYPE = 40
PRICE = 44
TIME_IN_FORCE = 59
TRANSACT_TIME = 60
LAST_PX = 31
LAST_QTY = 32
LEAVES_QTY = 151
CUM_QTY = 14
AVG_PX = 6
ORD_REJ_REASON = 103

# Market data
MD_REQ_ID = 262
SUBSCRIPTION_REQUEST_TYPE = 263
MARKET_DEPTH = 264
MD_UPDATE_TYPE = 265
NO_MD_ENTRY_TYPES = 267
MD_ENTRY_TYPE = 269
NO_RELATED_SYM = 146
NO_MD_ENTRIES = 268
MD_ENTRY_PX = 270
MD_ENTRY_SIZE = 271
MD_UPDATE_ACTION = 279
MD_ENTRY_ID = 278

# Valores de MsgType
LOGON = "A"
LOGOUT = "5"
HEARTBEAT = "0"
TEST_REQUEST = "1"
RESEND_REQUEST = "2"
REJECT = "3"
SEQUENCE_RESET = "4"
NEW_ORDER_SINGLE = "D"
ORDER_CANCEL_REQUEST = "F"
EXECUTION_REPORT = "8"
MARKET_DATA_REQUEST = "V"
MARKET_DATA_SNAPSHOT = "W"
MARKET_DATA_INCREMENTAL = "X"
MARKET_DATA_REJECT = "Y"
BUSINESS_MESSAGE_REJECT = "j"

SESSION_TYPES = {LOGON, LOGOUT, HEARTBEAT, TEST_REQUEST, RESEND_REQUEST, REJECT, SEQUENCE_RESET}


class FixError(ValueError):
    """Mensagem malformada ou inconsistente."""


def utc_timestamp(now: Optional[datetime] = None) -> str:
    """Formato FIX UTCTimestamp com milissegundos."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%d-%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


class FixMessage:
    """Lista ordenada de pares tag=valor, com acesso por tag."""

    __slots__ = ("fields",)

    def __init__(self, msg_type: Optional[str] = None, fields: Optional[Iterable] = None):
        self.fields: list[tuple[int, str]] = [(int(t), str(v)) for t, v in (fields or [])]
        if msg_type is not None:
            self.set(MSG_TYPE, msg_type)

    # ------------------------------------------------------------- acesso
    def set(self, tag: int, value) -> "FixMessage":
        tag = int(tag)
        value = str(value)
        for i, (existing, _) in enumerate(self.fields):
            if existing == tag:
                self.fields[i] = (tag, value)
                return self
        self.fields.append((tag, value))
        return self

    def append(self, tag: int, value) -> "FixMessage":
        """Adiciona sem substituir — necessário em grupos repetitivos."""
        self.fields.append((int(tag), str(value)))
        return self

    def get(self, tag: int, default: Optional[str] = None) -> Optional[str]:
        for existing, value in self.fields:
            if existing == tag:
                return value
        return default

    def get_all(self, tag: int) -> list[str]:
        return [value for existing, value in self.fields if existing == tag]

    def get_int(self, tag: int, default: int = 0) -> int:
        try:
            return int(self.get(tag, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, tag: int, default: float = 0.0) -> float:
        try:
            return float(self.get(tag, default))
        except (TypeError, ValueError):
            return default

    def has(self, tag: int) -> bool:
        return any(existing == tag for existing, _ in self.fields)

    @property
    def msg_type(self) -> str:
        return self.get(MSG_TYPE, "")

    @property
    def seq_num(self) -> int:
        return self.get_int(MSG_SEQ_NUM)

    @property
    def is_session(self) -> bool:
        return self.msg_type in SESSION_TYPES

    def groups(self, count_tag: int, delimiter_tag: int) -> Iterator[dict[int, str]]:
        """Percorre um grupo repetitivo (ex.: 268 NoMDEntries, delimitador 269)."""
        collecting = False
        current: dict[int, str] = {}
        for tag, value in self.fields:
            if tag == count_tag:
                collecting = True
                continue
            if not collecting:
                continue
            if tag == delimiter_tag:
                if current:
                    yield current
                current = {tag: value}
            elif current:
                if tag in current:  # tag repetida fora do grupo: acabou
                    yield current
                    current = {}
                    collecting = False
                    continue
                current[tag] = value
        if current:
            yield current

    # ------------------------------------------------------------ serialização
    def encode(
        self,
        begin_string: str = "FIX.4.4",
        sender: str = "",
        target: str = "",
        seq_num: int = 1,
        sending_time: Optional[str] = None,
    ) -> bytes:
        """Monta o corpo com cabeçalho, BodyLength e CheckSum corretos."""
        header = [(MSG_TYPE, self.msg_type)]
        if sender:
            header.append((SENDER_COMP_ID, sender))
        if target:
            header.append((TARGET_COMP_ID, target))
        header.append((MSG_SEQ_NUM, str(seq_num)))
        header.append((SENDING_TIME, sending_time or utc_timestamp()))

        reserved = {MSG_TYPE, SENDER_COMP_ID, TARGET_COMP_ID, MSG_SEQ_NUM, SENDING_TIME,
                    BEGIN_STRING, BODY_LENGTH, CHECKSUM}
        body_fields = header + [(t, v) for t, v in self.fields if t not in reserved]
        body = "".join(f"{tag}={value}{SOH}" for tag, value in body_fields)
        prefix = f"{BEGIN_STRING}={begin_string}{SOH}{BODY_LENGTH}={len(body)}{SOH}"
        without_checksum = prefix + body
        return (without_checksum + f"{CHECKSUM}={checksum(without_checksum)}{SOH}").encode("ascii")

    @classmethod
    def decode(cls, raw: bytes | str, validate: bool = True) -> "FixMessage":
        """Lê uma mensagem completa; valida BodyLength e CheckSum se pedido."""
        text = raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else raw
        if not text.endswith(SOH):
            raise FixError("mensagem não termina com SOH")
        fields: list[tuple[int, str]] = []
        for chunk in text.split(SOH):
            if not chunk:
                continue
            tag, sep, value = chunk.partition("=")
            if not sep:
                raise FixError(f"campo sem '=': {chunk!r}")
            try:
                fields.append((int(tag), value))
            except ValueError as exc:
                raise FixError(f"tag não numérica: {tag!r}") from exc
        if not fields:
            raise FixError("mensagem vazia")
        message = cls(fields=fields)
        if validate:
            _validate(text, message)
        return message


def checksum(payload: str) -> str:
    return f"{sum(payload.encode('ascii')) % 256:03d}"


def _validate(text: str, message: FixMessage) -> None:
    if message.fields[0][0] != BEGIN_STRING:
        raise FixError("primeiro campo deve ser 8 (BeginString)")
    if message.fields[-1][0] != CHECKSUM:
        raise FixError("último campo deve ser 10 (CheckSum)")
    marker = f"{SOH}{CHECKSUM}="
    cut = text.rfind(marker)
    if cut < 0:
        raise FixError("CheckSum ausente")
    without_checksum = text[: cut + 1]
    expected = checksum(without_checksum)
    if message.get(CHECKSUM) != expected:
        raise FixError(f"CheckSum inválido: {message.get(CHECKSUM)} != {expected}")
    body_start = without_checksum.find(SOH, without_checksum.find(f"{SOH}{BODY_LENGTH}=") + 1) + 1
    declared = message.get_int(BODY_LENGTH, -1)
    actual = len(without_checksum) - body_start
    if declared != actual:
        raise FixError(f"BodyLength inválido: {declared} != {actual}")


def split_stream(buffer: str) -> tuple[list[str], str]:
    """Separa mensagens completas do buffer TCP; devolve (mensagens, resto).

    Usa o BodyLength declarado para achar o fim exato de cada mensagem — não dá
    para confiar em delimitador, já que SOH aparece dentro do corpo.
    """
    messages: list[str] = []
    while True:
        start = buffer.find(f"{BEGIN_STRING}=")
        if start < 0:
            return messages, ""
        length_marker = buffer.find(f"{SOH}{BODY_LENGTH}=", start)
        if length_marker < 0:
            return messages, buffer[start:]
        body_start = buffer.find(SOH, length_marker + 1)
        if body_start < 0:
            return messages, buffer[start:]
        try:
            body_length = int(buffer[length_marker + 3 : body_start])
        except ValueError:
            buffer = buffer[start + 2 :]  # cabeçalho corrompido: procura o próximo
            continue
        end = body_start + 1 + body_length
        tail = buffer.find(SOH, end)  # campo 10=xxx
        if tail < 0 or len(buffer) < tail + 1:
            return messages, buffer[start:]
        messages.append(buffer[start : tail + 1])
        buffer = buffer[tail + 1 :]
