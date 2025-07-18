from typing import *
import asyncio
import socket
import logging

from base_cipher import Cipher
import logger_setup


class Socks5Client:
    def __init__(self, cipher: Optional[Cipher] = None, udp_cipher: Optional[Cipher] = None, log_bytes: bool = True):
        self.socks_version = 5
        self.cipher = Cipher if cipher is None else cipher
        self.udp_cipher = Cipher if udp_cipher is None else udp_cipher
        self.log_bytes = log_bytes # only after handshake
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False
        self.bytes_sent = 0
        self.bytes_received = 0
        self.logger = logging.getLogger(__name__)

        self.user_commands = {
            'connect': 0x01,
            'bind': 0x02,
            'associate': 0x03,
        }
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._host = None
        self._port = None
        self._proxy_host = None
        self._proxy_port = None
        self._udp_proxy_host = None
        self._udp_proxy_port = None
        self._loop = asyncio.new_event_loop()
        self._pt_buffer = bytearray()
        asyncio.set_event_loop(self._loop)

    async def handshake(self, proxy_host: str = '127.0.0.1', proxy_port: int = 1080,
                        username: Optional[str] = None, password: Optional[str] = None):
        self.reader, self.writer = await asyncio.open_connection(proxy_host, proxy_port)
        self.logger.info(f"Connected to SOCKS5 proxy at {proxy_host}:{proxy_port}")

        methods = [0x00]
        if username and password:
            methods.insert(0, 0x02)

        methods_msg = await self.cipher.client_send_methods(self.socks_version, methods)
        await self.asend(methods_msg, encrypt=False, log_bytes=False)
        method_chosen = await self.cipher.client_get_method(self.reader)

        if method_chosen == 0xFF:
            raise ConnectionError("No acceptable authentication methods.")

        if method_chosen == 0x02:
            if not username or not password:
                raise ConnectionError("Proxy requires username/password authentication, but none provided")

            auth_ok = await self.cipher.client_auth_userpass(username, password, self.reader, self.writer)
            if not auth_ok:
                raise ConnectionError("Authentication failed")
            self.logger.info("Authenticated successfully")

        elif method_chosen == 0x00:
            self.logger.info("No authentication required by proxy")

        else:
            raise ConnectionError(f"Unsupported authentication method selected by proxy: {method_chosen}")

    async def async_connect(self, target_host: str, target_port: int,
                            proxy_host: str = '127.0.0.1', proxy_port: int = 1080,
                            username: Optional[str] = None, password: Optional[str] = None):

        await self.handshake(proxy_host=proxy_host, proxy_port=proxy_port, username=username, password=password)

        cmd_bytes = await self.cipher.client_command(
            self.socks_version, self.user_commands['connect'], target_host, target_port
        )
        await self.asend(cmd_bytes, encrypt=False, log_bytes=False)
        address, port = await self.cipher.client_connect_confirm(self.reader)

        self._host = target_host
        self._port = target_port
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self.connected = True
        self.logger.info(f"Connected to {target_host}:{target_port} through proxy")

    def connect(self, target_host: str, target_port: int,
                proxy_host: str = '127.0.0.1', proxy_port: int = 1080,
                username: Optional[str] = None, password: Optional[str] = None):
        return self._loop.run_until_complete(self.async_connect(target_host, target_port,
                                    proxy_host=proxy_host, proxy_port=proxy_port, username=username, password=password))

    async def async_udp_associate(self, target_host: str, target_port: int,
                            proxy_host: str = '127.0.0.1', proxy_port: int = 1080,
                            username: Optional[str] = None, password: Optional[str] = None) -> Tuple[str, str]:

        await self.handshake(proxy_host=proxy_host, proxy_port=proxy_port, username=username, password=password)

        cmd_bytes = await self.cipher.client_command(
            self.socks_version, self.user_commands['associate'], target_host, target_port
        )
        await self.asend(cmd_bytes, encrypt=False, log_bytes=False)

        udp_host, udp_port = await self.cipher.client_connect_confirm(self.reader)

        self._host = target_host
        self._port = target_port
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._udp_proxy_host = udp_host
        self._udp_proxy_port = udp_port

        return udp_host, udp_port

    def udp_associate(self, target_host: str, target_port: int,
                proxy_host: str = '127.0.0.1', proxy_port: int = 1080,
                username: Optional[str] = None, password: Optional[str] = None):
        return self._loop.run_until_complete(self.async_udp_associate(target_host, target_port,
                                    proxy_host=proxy_host, proxy_port=proxy_port, username=username, password=password))


    async def asend(self, data: bytes, encrypt: bool = True, log_bytes: bool = True, wait: bool = True):
        data = self.cipher.encrypt(data) if encrypt else data
        if self.log_bytes and log_bytes:
            self.bytes_sent += len(data)
        self.writer.write(data)
        if wait:
            await self.writer.drain()

    def send(self, data: bytes, encrypt: bool = True):
        return self._loop.run_until_complete(self.asend(data, encrypt=encrypt))


    async def aread(self, num_bytes: int = -1, decrypt: bool = True,
                    log_bytes: bool = True, **kwargs) -> bytes:
        # "num_bytes == -1" - means that aread will return every byte before the connection is closed

        if num_bytes < -1 or num_bytes == 0:
            return b''

        buffer_length = len(self._pt_buffer)
        if num_bytes == -1:
            data = await self.reader.read(-1)
            if self.log_bytes and log_bytes:
                self.bytes_received += len(data)
            data = self._pt_buffer + (self.cipher.decrypt(data, **kwargs) if decrypt and data else data)
            self._pt_buffer = bytearray()
        elif num_bytes == buffer_length:
            data = self._pt_buffer
            self._pt_buffer = bytearray()
        elif num_bytes > buffer_length:
            data = await self.reader.read(num_bytes - buffer_length)
            if self.log_bytes and log_bytes:
                self.bytes_received += len(data)
            data = self._pt_buffer + (self.cipher.decrypt(data, **kwargs) if decrypt and data else data)
            self._pt_buffer = bytearray()
        else:
            data = self._pt_buffer[:num_bytes]
            del self._pt_buffer[:num_bytes]
        return data

    def read(self, num_bytes: int = -1, **kwargs) -> bytes:
        return self._loop.run_until_complete(self.aread(num_bytes, **kwargs))

    async def areadexactly(self, num_bytes: int, decrypt: bool = True, log_bytes: bool = True, **kwargs) -> bytes:
        buffer_length = len(self._pt_buffer)
        if num_bytes == buffer_length:
            data = self._pt_buffer
            self._pt_buffer = bytearray()
        elif num_bytes > buffer_length:
            data = await self.reader.readexactly(num_bytes - buffer_length)
            if self.log_bytes and log_bytes:
                self.bytes_received += len(data)
            data = self._pt_buffer + (self.cipher.decrypt(data, **kwargs) if decrypt else data)
            self._pt_buffer = bytearray()
        else:
            data = self._pt_buffer[:num_bytes]
            del self._pt_buffer[:num_bytes]
        return data

    def readexactly(self, num_bytes: int, **kwargs) -> bytes:
        return self._loop.run_until_complete(self.areadexactly(num_bytes, **kwargs))

    async def areaduntil(self, sep: Union[str, bytes] = '\n', decrypt: bool = True, log_bytes: bool = True,
                         bytes_block: int = 1024, limit: int = 65535, **kwargs) -> bytes:
        sep = sep.encode() if isinstance(sep, str) else sep

        pos = self._pt_buffer.find(sep)
        if pos != -1:
            data = self._pt_buffer[:pos + len(sep)]
            del self._pt_buffer[:pos + len(sep)]
            return data

        if not decrypt:
            try:
                data = await (self.reader.readline() if sep == b'\n' else self.reader.readuntil(sep))
            except asyncio.IncompleteReadError as e:
                data = e.partial
            if self.log_bytes and log_bytes:
                self.bytes_received += len(data)
            return self._pt_buffer + data

        while True:
            chunk = await self.reader.read(bytes_block)
            if not chunk:
                break

            if self.log_bytes and log_bytes:
                self.bytes_received += len(chunk)

            self._pt_buffer += self.cipher.decrypt(chunk, **kwargs)

            pos = self._pt_buffer.find(sep)
            if pos != -1:
                data = self._pt_buffer[:pos + len(sep)]
                del self._pt_buffer[:pos + len(sep)]
                return data

        data = self._pt_buffer
        self._pt_buffer = bytearray()
        return data

    def readuntil(self, **kwargs) -> bytes:
        return self._loop.run_until_complete(self.areaduntil(**kwargs))

    async def areadline(self, log_bytes: bool = True, decrypt: bool = True, **kwargs) -> bytes:
        if 'sep' in kwargs.keys():
            kwargs.pop('sep')
        return await self.areaduntil(sep='\n', decrypt=decrypt, log_bytes=log_bytes, **kwargs)

    def readline(self, **kwargs) -> bytes:
        return self._loop.run_until_complete(self.areadline(**kwargs))


    async def async_close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
            self.connected = False
            self.logger.info("Connection closed.")

    def close(self):
        self._loop.run_until_complete(self.async_close())
        self._loop.stop()
        self._loop.close()

    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.async_close()

    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __str__(self):
        connection = f'connected="{self._host}:{self._port}" proxy="{self._proxy_host}:{self._proxy_port}"'
        return f'{self.__class__.__name__}({connection if self.connected else "waiting"}, cipher={self.cipher})'