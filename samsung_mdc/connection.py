from typing import Union, Sequence, Tuple
from functools import partial
from enum import Enum
import asyncio

from .exceptions import MDCResponseError, MDCTimeoutError, MDCTLSRequired
from .utils import repr_hex


HEADER_CODE = 0xAA
RESPONSE_CMD = 0xFF
ACK_CODE = ord('A')  # 0x41 65
NAK_CODE = ord('N')  # 0x4E 78


async def wait_for(aw, timeout, reason):
    try:
        return await asyncio.wait_for(aw, timeout)
    except asyncio.TimeoutError as exc:
        raise MDCTimeoutError(reason) from exc


class CONNECTION_MODE(Enum):
    TCP = 'tcp'
    SERIAL = 'serial'


class MDCConnection:
    reader, writer = None, None

    def __init__(self, target, mode=CONNECTION_MODE.TCP, timeout=5,
                 connect_timeout=None, verbose=False, **connection_kwargs):
        self.target = target
        self.mode = CONNECTION_MODE(mode)
        self.connection_kwargs = connection_kwargs

        self.timeout = timeout
        self.connect_timeout = connect_timeout or timeout
        self.verbose = (
            partial(print, self.target) if verbose is True else verbose)

    async def open(self):
        if self.mode == CONNECTION_MODE.TCP:
            if isinstance(self.target, (list, tuple)):
                # make target be compatible with socket.__init__
                target, port = self.target
            else:
                target, *port = self.target.split(':')
                port = port and int(port[0]) or 1515
            self.connection_kwargs.setdefault('port', port)

            self.reader, self.writer = \
                await wait_for(
                    asyncio.open_connection(target, **self.connection_kwargs),
                    self.connect_timeout, 'Connect timeout')

            # TODO: implement TLS
            # https://stackoverflow.com/questions/62851407/how-do-i-enable-tls-on-an-already-connected-python-asyncio-stream

            # import ssl
            # resp = await wait_for(self.reader.read(15), self.timeout,
            #                       'Response TLS header read timeout')
            # if resp == b'MDCSTART<<TLS>>':
            #     transport = self.writer.transport
            #     protocol = transport.get_protocol()
            #     loop = asyncio.get_event_loop()
            #     ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            #     ssl_ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
            #     ssl_ctx.check_hostname = False
            #     ssl_ctx.verify_mode = ssl.VerifyMode.CERT_NONE
            #     ssl_ctx.verify_mode = ssl.VerifyMode.CERT_REQUIRED
            #     ssl_ctx.load_cert_chain('example.cer', 'example.key')
            #     # ssl_ctx.load_verify_locations('example.cer')
            #     ssl_transport = await loop.start_tls(
            #         transport, protocol, ssl_ctx)
            #     self.writer._transport = ssl_transport
            #     self.reader._transport = ssl_transport
        else:
            # Make this package optional
            from serial_asyncio import open_serial_connection

            self.reader, self.writer = \
                await wait_for(
                    open_serial_connection(
                        url=self.target,
                        **self.connection_kwargs),
                    self.connect_timeout, 'Connect timeout')

        if self.verbose:
            self.verbose('Connected')

    @property
    def is_opened(self):
        return self.writer is not None

    async def send(self, cmd: Union[int, Tuple[int], Tuple[int, int]], id: int,
                   data: Union[bytes, Sequence] = b''):
        if isinstance(cmd, int):
            cmd = (cmd,)
        payload = (
            bytes((HEADER_CODE, cmd[0], id, len(cmd[1:]) + len(data)))
            + bytes(cmd[1:]) + bytes(data))
        checksum = sum(payload[1:]) % 256
        payload += bytes((checksum,))

        if not self.is_opened:
            await self.open()

        if self.reader._buffer:
            print(self.reader._buffer)
        self.writer.write(payload)
        await wait_for(self.writer.drain(), self.timeout, 'Write timeout')
        if self.verbose:
            self.verbose('Sent', repr_hex(payload))

        resp = await wait_for(self.reader.read(4), self.timeout,
                              'Response header read timeout')
        if not resp:
            raise MDCResponseError('Empty response', resp)
        if resp[0] != HEADER_CODE:
            if (resp + self.reader._buffer) == b'MDCSTART<<TLS>>':
                raise MDCTLSRequired(resp + self.reader._buffer)
            raise MDCResponseError('Unexpected header',
                                   resp + self.reader._buffer)
        if resp[1] != RESPONSE_CMD:
            resp += self.reader._buffer
            raise MDCResponseError('Unexpected cmd',
                                   resp + self.reader._buffer)
        if resp[2] != id:
            resp += self.reader._buffer
            raise MDCResponseError('Unexpected id', resp + self.reader._buffer)
        resp += await wait_for(self.reader.read(resp[3] + 1), self.timeout,
                               'Response data read timeout')
        if self.verbose:
            self.verbose('Recv', repr_hex(resp))

        checksum = sum(resp[1:-1]) % 256
        if checksum != int(resp[-1]):
            raise MDCResponseError('Checksum failed', resp)

        ack, rcmd, data = resp[4], resp[5], resp[6:-1]
        if ack not in (ACK_CODE, NAK_CODE):
            raise MDCResponseError('Unexpected ACK/NAK', resp)

        return (
            ack == ACK_CODE,
            (rcmd, data[0]) if (ack == ACK_CODE and len(cmd) > 1) else (rcmd,),
            data[1:] if (ack == ACK_CODE and len(cmd) > 1) else data
        )

    async def close(self):
        writer = self.writer
        self.reader, self.writer = None, None
        writer.close()
        await writer.wait_closed()

    async def __aenter__(self):
        if not self.is_opened:
            await self.open()
        return self

    async def __aexit__(self, *args):
        if self.is_opened:
            await self.close()

    def __await__(self):
        return self.__aenter__().__await__()
