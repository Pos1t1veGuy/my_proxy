from typing import *
import os
import hashlib
import asyncio
import struct
import socket
import math
import ipaddress as ipa

import cryptography.exceptions
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter
from Cryptodome.Util.Padding import pad, unpad
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from ..base_cipher import Cipher, REPLYES


class AES_CTR(Cipher):
    def __init__(self, key: bytes, iv: Optional[bytes] = None, iv_length: int = 16, **kwargs):
        super().__init__(key, iv=iv, **kwargs)
        self.key = key
        self.iv = iv
        self.iv_length = iv_length
        self.encryptor = None
        self.decryptor = None

        if iv is not None:
            self._init_ciphers(iv)

    def _init_ciphers(self, iv: bytes):
        ctr_enc = Counter.new(128, initial_value=int.from_bytes(iv, byteorder='big'))
        ctr_dec = Counter.new(128, initial_value=int.from_bytes(iv, byteorder='big'))

        self.encryptor = AES.new(self.key, AES.MODE_CTR, counter=ctr_enc)
        self.decryptor = AES.new(self.key, AES.MODE_CTR, counter=ctr_dec)
        self.iv = iv

    async def client_send_methods(self, socks_version: int, methods: List[int]) -> List[bytes]:
        if self.iv is None:
            raise ValueError("IV must be initialized before sending methods")

        header = struct.pack("!BB", socks_version, len(methods))

        methods_bytes = struct.pack(f"!{len(methods)}B", *methods)
        return [self.iv + b''.join(self.encrypt(header + methods_bytes))]

    async def server_get_methods(self, socks_version: int, reader: asyncio.StreamReader) -> Dict[str, bool]:
        iv = await reader.readexactly(self.iv_length)
        self._init_ciphers(iv)

        version, nmethods = struct.unpack("!BB", b''.join(self.decrypt(await reader.readexactly(2))))

        if version != socks_version:
            raise ConnectionError(f"Unsupported SOCKS version: {version}")

        encrypted_methods = await reader.readexactly(nmethods)
        methods = b''.join(self.decrypt(encrypted_methods))

        return {
            'supports_no_auth': 0x00 in methods,
            'supports_gss_api': 0x01 in methods,
            'supports_user_pass': 0x02 in methods
        }

    async def server_send_method_to_user(self, socks_version: int, method: int) -> List[bytes]:
        return self.encrypt(struct.pack("!BB", socks_version, method))

    async def client_get_method(self, socks_version: int, reader: asyncio.StreamReader) -> int:
        enc = await reader.readexactly(2)
        version, method = struct.unpack("!BB", b''.join(self.decrypt(enc)))

        if version != socks_version:
            raise ConnectionError(f"Unsupported SOCKS version: {version}")
        if method == 0xFF:
            raise ConnectionError("No acceptable authentication methods.")

        return method

    async def server_auth_userpass(self, logins: Dict[str, str], reader: asyncio.StreamReader,
                                   writer: asyncio.StreamWriter) -> Optional[Tuple[str, str]]:
        encrypted_header = await reader.readexactly(2)
        auth_version, ulen = struct.unpack("!BB", b''.join(self.decrypt(encrypted_header)))

        username = await reader.readexactly(ulen)
        username = b''.join(self.decrypt(username)).decode()

        plen_encrypted = await reader.readexactly(1)
        plen = b''.join(self.decrypt(plen_encrypted))[0]

        password = await reader.readexactly(plen)
        password = b''.join(self.decrypt(password)).decode()

        if logins.get(username) == password:
            writer.write(b''.join(self.encrypt(struct.pack("!BB", 1, 0))))
            await writer.drain()
            return username, password
        else:
            writer.write(b''.join(self.encrypt(struct.pack("!BB", 1, 1))))
            await writer.drain()

    async def client_auth_userpass(self, username: str, password: str, reader: asyncio.StreamReader,
                                   writer: asyncio.StreamWriter) -> bool:
        username_bytes = username.encode()
        password_bytes = password.encode()

        writer.write(b''.join(self.encrypt(struct.pack("!BB", 1, len(username_bytes)))))
        writer.write(b''.join(self.encrypt(username_bytes)))
        writer.write(b''.join(self.encrypt(bytes([len(password_bytes)]))))
        writer.write(b''.join(self.encrypt(password_bytes)))
        await writer.drain()

        response = await reader.readexactly(2)
        version, status = struct.unpack("!BB", b''.join(self.decrypt(response)))

        if status != 0:
            raise ConnectionError("Authentication failed")

        return True

    async def client_command(self, socks_version: int, user_command: int, target_host: str, target_port: int) -> List[bytes]:
        try:
            ip = ipa.ip_address(target_host)
            if ip.version == 4: # IPv4
                atyp = 0x01
                addr_part = ip.packed
            else: # IPv6
                atyp = 0x04
                addr_part = ip.packed
        except ValueError: # domain
            atyp = 0x03
            addr_bytes = target_host.encode("idna")
            length = len(addr_bytes)
            if length > 255:
                raise ValueError("Domain name too long for SOCKS5")
            addr_part = struct.pack("!B", length) + addr_bytes

        return self.encrypt(
            struct.pack("!BBBB", socks_version, user_command, 0x00, atyp) + addr_part + struct.pack("!H", target_port)
        )

    async def server_handle_command(self, socks_version: int, user_command_handlers: Dict[int, Callable],
                                    reader: asyncio.StreamReader) -> Tuple[str, int, Callable]:

        version, cmd, rsv, address_type = b''.join(self.decrypt(await reader.readexactly(4)))
        if version != socks_version:
            raise ConnectionError(f"Unsupported SOCKS version: {version}")

        if not cmd in user_command_handlers.keys():
            raise ConnectionError(f"Unsupported command: {cmd}, it must be one of {list(user_command_handlers.keys())}")
        cmd = user_command_handlers[cmd]

        match address_type:
            case 0x01:  # IPv4
                data = await reader.readexactly(4 + 2)
                data = b''.join(self.decrypt(data))
                addr = '.'.join(map(str, data[:4]))
                port = int.from_bytes(data[4:], 'big')

            case 0x03:  # domain
                domain_len = b''.join(self.decrypt(await reader.readexactly(1)))[0]
                data = b''.join(self.decrypt(await reader.readexactly(domain_len + 2)))
                addr = data[:domain_len].decode()
                port = int.from_bytes(data[domain_len:], 'big')

            case 0x04:  # IPv6
                data = b''.join(self.decrypt(await reader.readexactly(16 + 2)))
                addr = socket.inet_ntop(socket.AF_INET6, data[:16])
                port = int.from_bytes(data[16:], 'big')

            case _:
                raise ConnectionError(f"Invalid address: {address_type}, it must be 0x01/0x03/0x04")

        return addr, port, cmd

    async def server_make_reply(self, socks_version: int, reply_code: int, address: str = '0', port: int = 0) -> List[bytes]:
        return self.encrypt(
            b''.join(await super().server_make_reply(socks_version, reply_code, address=address, port=port))
        )

    async def client_connect_confirm(self, reader: asyncio.StreamReader) -> Tuple[str, str]:
        hdr = b''.join(self.decrypt(await reader.readexactly(4)))
        ver, rep, rsv, atyp = hdr

        if ver != 0x05:
            raise ConnectionError(f"Invalid SOCKS version in reply: {ver}")
        if rep != 0x00:
            raise ConnectionError(f"SOCKS5 request failed {REPLYES[rep]}")

        match atyp:
            case 0x01:  # IPv4
                addr_port = b''.join(self.decrypt(await reader.readexactly(4 + 2)))
                addr_bytes, port_bytes = addr_port[:4], addr_port[4:]
                address = socket.inet_ntoa(addr_bytes)
            case 0x03:  # Domain
                len_byte = await reader.readexactly(1)
                domain_len = b''.join(self.decrypt(len_byte))[0]
                addr_port = b''.join(self.decrypt(await reader.readexactly(domain_len + 2)))
                addr_bytes, port_bytes = addr_port[:domain_len], addr_port[domain_len:]
                address = addr_bytes.decode('idna')
            case 0x04:  # IPv6
                addr_port = b''.join(self.decrypt(await reader.readexactly(16 + 2)))
                addr_bytes, port_bytes = addr_port[:16], addr_port[16:]
                address = socket.inet_ntop(socket.AF_INET6, addr_bytes)
            case _:
                raise ConnectionError(f"Invalid ATYP in reply: {atyp}")

        return address, struct.unpack('!H', port_bytes)[0]


    def encrypt(self, data: bytes) -> List[bytes]:
        if not self.encryptor is None:
            return [self.wrapper.wrap(self.encryptor.encrypt(data))]
        else:
            raise OSError(f'{self.__class__.__name__} needs to specify IV (init vector) in constructor or handshake')

    def decrypt(self, data: bytes) -> List[bytes]:
        if not self.encryptor is None:
            return [self.decryptor.decrypt(self.wrapper.wrap(data))]
        else:
            raise OSError(f'{self.__class__.__name__} needs to specify IV (init vector) in constructor or handshake')


class AES_CBC(Cipher):
    def __init__(self, key: bytes, iv: Optional[bytes] = None, iv_length: int = 16, **kwargs):
        super().__init__(key, iv=iv, **kwargs)
        self.key = key
        self.iv = iv
        self.iv_length = iv_length
        self.encryptor = None
        self.decryptor = None

        if iv is not None:
            self._init_ciphers(iv)

    def _init_ciphers(self, iv: bytes):
        self.encryptor = AES.new(self.key, AES.MODE_CBC, iv=iv)
        self.decryptor = AES.new(self.key, AES.MODE_CBC, iv=iv)
        self.iv = iv

    async def client_send_methods(self, socks_version: int, methods: List[int]) -> List[bytes]:
        if self.iv is None:
            raise ValueError("IV must be initialized before sending methods")

        header = struct.pack("!BB", socks_version, len(methods))

        methods_bytes = struct.pack(f"!{len(methods)}B", *methods)
        return [self.iv, b''.join(self.encrypt(header + methods_bytes))]

    async def server_get_methods(self, socks_version: int, reader: asyncio.StreamReader) -> Dict[str, bool]:
        iv = await reader.readexactly(self.iv_length)
        self._init_ciphers(iv)

        encrypted_header = await reader.readexactly(AES.block_size)
        header = b''.join(self.decrypt(encrypted_header))
        version, nmethods = header[:2]
        methods = header[2:]

        if version != socks_version:
            raise ConnectionError(f"Unsupported SOCKS version: {version}")

        if nmethods > AES.block_size-2:
            padded_len = ((nmethods + AES.block_size - 15) // AES.block_size) * AES.block_size
            encrypted_methods = await reader.readexactly(padded_len)
            methods += b''.join(self.decrypt(encrypted_methods))

        return {
            'supports_no_auth': 0x00 in methods,
            'supports_gss_api': 0x01 in methods,
            'supports_user_pass': 0x02 in methods
        }

    async def server_send_method_to_user(self, socks_version: int, method: int) -> List[bytes]:
        return self.encrypt(struct.pack("!BB", socks_version, method))

    async def client_get_method(self, socks_version: int, reader: asyncio.StreamReader) -> int:
        decrypted_response = b''.join(self.decrypt(await reader.readexactly(AES.block_size)))
        version, method = struct.unpack("!BB", decrypted_response[:2])

        if version != socks_version:
            raise ConnectionError(f"Unsupported SOCKS version: {version}")
        if method == 0xFF:
            raise ConnectionError("No acceptable authentication methods.")

        return method

    async def server_auth_userpass(self, logins: Dict[str, str], reader: asyncio.StreamReader,
                                   writer: asyncio.StreamWriter) -> Optional[Tuple[str, str]]:
        header = b''.join(self.decrypt(await reader.readexactly(AES.block_size)))
        version, ulen = struct.unpack("!BB", header[:2])

        padded_ulen = ((ulen + AES.block_size - 1) // AES.block_size) * AES.block_size
        username = b''.join(self.decrypt(await reader.readexactly(padded_ulen)))
        username = username[:ulen].decode()

        plen = b''.join(self.decrypt(await reader.readexactly(AES.block_size)))[0]

        padded_plen = ((plen + AES.block_size - 1) // AES.block_size) * AES.block_size
        decrypted_password = b''.join(self.decrypt(await reader.readexactly(padded_plen)))
        password = decrypted_password[:plen].decode()

        if logins.get(username) == password:
            writer.write(b''.join(self.encrypt(struct.pack("!BB", 1, 0))))
            await writer.drain()
            return username, password
        else:
            writer.write(b''.join(self.encrypt(struct.pack("!BB", 1, 1))))
            await writer.drain()

    async def client_auth_userpass(self, username: str, password: str, reader: asyncio.StreamReader,
                                   writer: asyncio.StreamWriter) -> bool:
        username_bytes = username.encode()
        password_bytes = password.encode()

        writer.write(b''.join(self.encrypt(struct.pack("!BB", 1, len(username_bytes)))))
        writer.write(b''.join(self.encrypt(username_bytes)))
        writer.write(b''.join(self.encrypt(bytes([len(password_bytes)]))))
        writer.write(b''.join(self.encrypt(password_bytes)))
        await writer.drain()

        response = b''.join(self.decrypt(await reader.readexactly(AES.block_size)))
        version, status = struct.unpack("!BB", response)

        if status != 0:
            raise ConnectionError("Authentication failed")

        return True

    async def client_command(self, socks_version: int, user_command: int, target_host: str, target_port: int) -> bytes:
        addr_bytes = b''
        atyp = 0x01
        length = 4
        try:
            ip = ipa.ip_address(target_host)
            addr_part = ip.packed
            if ip.version == 6:  # IPv6
                atyp = 0x04
                length = 16
        except ValueError:  # domain
            atyp = 0x03
            addr_bytes = target_host.encode("idna")
            length = len(addr_bytes)
            if length > 255:
                raise ValueError("Domain name too long for SOCKS5")

        first_block = self.encrypt(
            struct.pack("!BBBBB", socks_version, user_command, 0x00, atyp, length)
        )
        second_block = self.encrypt(addr_bytes + struct.pack("!H", target_port))
        return first_block + second_block

    async def server_handle_command(self, socks_version: int, user_command_handlers: Dict[int, Callable],
                                        reader: asyncio.StreamReader) -> Tuple[str, int, Callable]:

        header_raw = await reader.readexactly(AES.block_size)
        header = b''.join(self.decrypt(header_raw))
        version, cmd, rsv, address_type, length = struct.unpack("!BBBBB", header[:5])
        if version != socks_version:
            raise ConnectionError(f"Unsupported SOCKS version: {version}")

        if not cmd in user_command_handlers.keys():
            raise ConnectionError(f"Unsupported command: {cmd}, it must be one of {list(user_command_handlers.keys())}")
        cmd = user_command_handlers[cmd]

        match address_type:
            case 0x01:  # IPv4
                encrypted = await reader.readexactly(AES.block_size)
                data = b''.join(self.decrypt(encrypted))
                addr = '.'.join(map(str, data[:4]))
                port = int.from_bytes(data[4:], 'big')

            case 0x03:  # domain
                total_len = length + 2
                padded_len = ((total_len + 15) // AES.block_size) * AES.block_size
                data = b''.join(self.decrypt(await reader.readexactly(padded_len)))
                addr = data[:length].decode()
                port = int.from_bytes(data[length:], 'big')

            case 0x04:  # IPv6
                data = b''.join(self.decrypt(await reader.readexactly(2*16)))
                addr = socket.inet_ntop(socket.AF_INET6, data[:16])
                port = int.from_bytes(data[16:], 'big')

            case _:
                raise ConnectionError(f"Invalid address: {address_type}, it must be 0x01/0x03/0x04")

        return addr, port, cmd

    async def server_make_reply(self, socks_version: int, reply_code: int, address: str = '0', port: int = 0) -> bytes:
        address_type = 0x01
        addr_data = socket.inet_aton("0.0.0.0")
        length = 4

        try:
            ip = ipa.ip_address(address)
            addr_data = ip.packed
            if ip.version == 6:
                address_type = 0x04
                length = 16

        except ValueError:
            address_type = 0x03
            addr_bytes = address.encode('idna')
            length = len(addr_bytes)
            if length > 255:
                raise ValueError("Domain name too long for SOCKS5 protocol")

            addr_data = bytes([length]) + addr_bytes

        except:
            address_type = 0x01
            port = 0

        first_header = struct.pack(
            "!BBBB",
            socks_version,
            reply_code,
            0x00,  # RSV
            address_type
        )
        second_header = struct.pack(
            f"!{length}sH",
            addr_data,
            port
        )
        return self.encrypt(first_header) + self.encrypt(second_header)

    async def client_connect_confirm(self, reader: asyncio.StreamReader) -> Tuple[str, str]:
        header_encrypted = await reader.readexactly(AES.block_size)
        header = b''.join(self.decrypt(header_encrypted))

        ver, rep, _, atyp = header
        if ver != 0x05:
            raise ConnectionError(f"Invalid SOCKS version in reply: {ver}")
        if rep != 0x00:
            raise ConnectionError(f"SOCKS5 CONNECT failed {REPLYES[rep]}")

        match atyp:
            case 0x01:  # IPv4
                addr_port = b''.join(self.decrypt(await reader.readexactly(AES.block_size)))
                addr_bytes, port_bytes = addr_port[:4], addr_port[4:6]
                address = socket.inet_ntoa(addr_bytes)

            case 0x03:  # Domain
                padded = ((1 + len(addr_bytes) + 2 + AES.block_size - 1) // AES.block_size) * AES.block_size
                addr_port = b''.join(self.decrypt(await reader.readexactly(padded)))
                domain_len = addr_port[0]
                addr_bytes = addr_port[1:1 + domain_len]
                port_bytes = addr_port[1 + domain_len:1 + domain_len + 2]
                address = addr_bytes.decode('idna')

            case 0x04:  # IPv6
                addr_port = b''.join(self.decrypt(await reader.readexactly(math.ceil(2*16))))
                addr_bytes, port_bytes = addr_port[:16], addr_port[16:]
                address = socket.inet_ntop(socket.AF_INET6, addr_bytes)

            case _:
                raise ConnectionError(f"Invalid ATYP in reply: {atyp}")

        return address, struct.unpack('!H', port_bytes)[0]


    def encrypt(self, data: bytes) -> List[bytes]:
        if not self.encryptor is None:
            res = []
            length = len(data)
            for i, chunk_start in enumerate(range(0, length, AES.block_size)):
                chunk = data[chunk_start:chunk_start + AES.block_size]
                if (i+1)*AES.block_size >= length:
                    chunk = pad(chunk, AES.block_size)
                res.append(self.encryptor.encrypt(chunk))
            return self.wrapper.wrap(res)
        else:
            raise OSError(f'{self.__class__.__name__} needs to specify IV (init vector) in constructor or handshake')

    def decrypt(self, data: bytes) -> List[bytes]:
        data = self.wrapper.unwrap(data)
        if not self.decryptor is None:
            res = []
            for chunk_start in range(0, len(data), AES.block_size):
                chunk = data[chunk_start:chunk_start + AES.block_size]
                res.append(self.decryptor.decrypt(chunk))
            res[-1] = unpad(res[-1], AES.block_size)
            return res
        else:
            raise OSError(f'{self.__class__.__name__} needs to specify IV (init vector) in constructor or handshake')


class ChaCha20_Poly1305(Cipher):
    def __init__(self, key: bytes, nonce_length: int = 12, **kwargs):
        super().__init__(key, nonce_length=nonce_length, **kwargs)
        self.key = key
        self.nonce_length = nonce_length
        self.cipher = ChaCha20Poly1305(key)
        self.nonce_counter = 0
        self.base_nonce = os.urandom(self.nonce_length-4)

        self.MAC_LENGTH = 16
        self._decoder_buffer = b''
        self.overhead_length = 2 + self.nonce_length + self.MAC_LENGTH

    async def client_send_methods(self, socks_version: int, methods: List[int]) -> bytes:
        first_block = self.encrypt(struct.pack("!BB", socks_version, len(methods)))
        second_block = self.encrypt(struct.pack(f"!{len(methods)}B", *methods))
        return first_block + second_block

    async def server_get_methods(self, socks_version: int, reader: asyncio.StreamReader) -> Dict[str, bool]:
        header = await reader.readexactly(2 + self.overhead_length)
        version, nmethods = struct.unpack("!BB", b''.join(self.decrypt(header)))

        if version != socks_version:
            raise ConnectionError(f"Unsupported SOCKS version: {version}")

        methods_enc = await reader.readexactly(nmethods + self.overhead_length)
        methods = struct.unpack(f"!{nmethods}B", b''.join(self.decrypt(methods_enc)))

        return {
            'supports_no_auth': 0x00 in methods,
            'supports_user_pass': 0x02 in methods
        }

    async def server_send_method_to_user(self, socks_version: int, method: int) -> bytes:
        return b''.join(self.encrypt(struct.pack("!BB", socks_version, method)))

    async def client_get_method(self, socks_version: int, reader: asyncio.StreamReader) -> int:
        enc = await reader.readexactly(2 + self.overhead_length)
        version, method = struct.unpack("!BB", b''.join(self.decrypt(enc)))

        if version != socks_version:
            raise ConnectionError(f"Unsupported SOCKS version: {version}")
        if method == 0xFF:
            raise ConnectionError("No acceptable authentication methods.")

        return method

    async def server_auth_userpass(self, logins: Dict[str, str], reader: asyncio.StreamReader,
                                   writer: asyncio.StreamWriter) -> Optional[Tuple[str, str]]:
        encrypted_header = await reader.readexactly(2 + self.overhead_length)
        auth_version, ulen = struct.unpack("!BB", b''.join(self.decrypt(encrypted_header)))

        username = await reader.readexactly(ulen + self.overhead_length)
        username = b''.join(self.decrypt(username)).decode()

        plen_encrypted = await reader.readexactly(1 + self.overhead_length)
        plen = b''.join(self.decrypt(plen_encrypted))[0]

        password = await reader.readexactly(plen + self.overhead_length)
        password = b''.join(self.decrypt(password)).decode()

        if logins.get(username) == password:
            writer.write(b''.join(self.encrypt(struct.pack("!BB", 1, 0))))
            await writer.drain()
            return username, password
        else:
            writer.write(b''.join(self.encrypt(struct.pack("!BB", 1, 1))))
            await writer.drain()

    async def client_auth_userpass(self, username: str, password: str, reader: asyncio.StreamReader,
                                   writer: asyncio.StreamWriter) -> bool:
        username_bytes = username.encode()
        password_bytes = password.encode()

        writer.write(b''.join(self.encrypt(struct.pack("!BB", 1, len(username_bytes)))))
        writer.write(b''.join(self.encrypt(username_bytes)))
        writer.write(b''.join(self.encrypt(bytes([len(password_bytes)]))))
        writer.write(b''.join(self.encrypt(password_bytes)))
        await writer.drain()

        response = await reader.readexactly(2 + self.overhead_length)
        version, status = struct.unpack("!BB", b''.join(self.decrypt(response)))

        if status != 0:
            raise ConnectionError("Authentication failed")

        return True

    async def client_command(self, socks_version: int, user_command: int, target_host: str, target_port: int) -> bytes:
        length = 4
        try:
            ip = ipa.ip_address(target_host)
            if ip.version == 4: # IPv4
                atyp = 0x01
                addr_part = ip.packed
            else: # IPv6
                atyp = 0x04
                length = 16
                addr_part = ip.packed
        except ValueError: # domain
            atyp = 0x03
            addr_part = target_host.encode("idna")
            length = len(addr_part)
            if length > 255:
                raise ValueError("Domain name too long for SOCKS5")

        first_block = self.encrypt(struct.pack("!BBBBB", socks_version, user_command, 0x00, atyp, length+2))
        second_block = self.encrypt(addr_part + struct.pack("!H", target_port))
        return first_block + second_block

    async def server_handle_command(self, socks_version: int, user_command_handlers: Dict[int, Callable],
                                    reader: asyncio.StreamReader) -> Tuple[str, int, Callable]:

        first_block = await reader.readexactly(5 + self.overhead_length)
        version, cmd, rsv, address_type, address_length = b''.join(self.decrypt(first_block))
        if version != socks_version:
            raise ConnectionError(f"Unsupported SOCKS version: {version}")

        if not cmd in user_command_handlers.keys():
            raise ConnectionError(f"Unsupported command: {cmd}, it must be one of {list(user_command_handlers.keys())}")
        cmd = user_command_handlers[cmd]


        data = b''.join(self.decrypt(await reader.readexactly(address_length + self.overhead_length)))
        match address_type:
            case 0x01:  # IPv4
                addr = '.'.join(map(str, data[:4]))
                port = int.from_bytes(data[4:], 'big')
            case 0x03:  # domain
                addr = data[:-2].decode()
                port = int.from_bytes(data[-2:], 'big')
            case 0x04:  # IPv6
                addr = socket.inet_ntop(socket.AF_INET6, data[:16])
                port = int.from_bytes(data[16:], 'big')
            case _:
                raise ConnectionError(f"Invalid address: {address_type}, it must be 0x01/0x03/0x04")

        return addr, port, cmd

    async def server_make_reply(self, socks_version: int, reply_code: int, address: str = '0', port: int = 0) -> bytes:
        address_type = 0x01
        addr_data = socket.inet_aton("0.0.0.0")
        length = 4

        try:
            ip = ipa.ip_address(address)

            if ip.version == 4:
                addr_data = ip.packed

            elif ip.version == 6:
                address_type = 0x04
                addr_data = ip.packed
                length = 16

        except addr_data:
            addr_bytes = address.encode('idna')
            length = len(addr_bytes)
            if length > 255:
                raise ValueError("Domain name too long for SOCKS5 protocol")
            address_type = 0x03

        except:
            address_type = 0x01
            port = 0

        first_block = self.encrypt(struct.pack(
            "!BBBBB",
            socks_version,
            reply_code,
            0x00,  # RSV
            address_type,
            length+2,
        ))
        second_block = self.encrypt(struct.pack(
            f"!{length}sH",
            addr_data,
            port
        ))
        return first_block + second_block

    async def client_connect_confirm(self, reader: asyncio.StreamReader) -> Tuple[str, str]:
        header_encrypted = await reader.readexactly(5 + self.overhead_length)
        header = b''.join(self.decrypt(header_encrypted))

        ver, rep, _, address_type, address_length = header
        if ver != 0x05:
            raise ConnectionError(f"Invalid SOCKS version in reply: {ver}")
        if rep != 0x00:
            raise ConnectionError(f"SOCKS5 CONNECT failed {REPLYES[rep]}")

        enc = await reader.readexactly(address_length + self.overhead_length)
        data = b''.join(self.decrypt(enc))
        match address_type:
            case 0x01:  # IPv4
                addr = '.'.join(map(str, data[:4]))
                port = int.from_bytes(data[4:], 'big')
            case 0x03:  # domain
                addr = data[:-2].decode()
                port = int.from_bytes(data[-2:], 'big')
            case 0x04:  # IPv6
                addr = socket.inet_ntop(socket.AF_INET6, data[:16])
                port = int.from_bytes(data[16:], 'big')
            case _:
                raise ConnectionError(f"Invalid address: {address_type}, it must be 0x01/0x03/0x04")

        return addr, port

    @property
    def nonce(self) -> bytes:
        if self.nonce_counter <= 0xFFFFFFFF:
            self.nonce_counter += 1
        else:
            self.nonce_counter = 0
            self.base_nonce = os.urandom(self.nonce_length-4)

        return self.base_nonce + self.nonce_counter.to_bytes(4, 'big')


    def encrypt(self, data: bytes) -> List[bytes]:
        result = []
        chunk_size = 65535

        for i in range(0, len(data), chunk_size):
            chunk = data[i:i + chunk_size]
            nonce = self.nonce
            result.append(len(chunk).to_bytes(2, byteorder='big') + nonce + self.cipher.encrypt(nonce, chunk, None))

        return self.wrapper.wrap(result)

    def decrypt(self, data: bytes) -> List[bytes]:
        data = self.wrapper.unwrap(data)
        if len(self._decoder_buffer) > 65535:
            self._decoder_buffer = b''
        self._decoder_buffer += data
        result = []

        while True:
            if len(self._decoder_buffer) < 2:
                break

            length = int.from_bytes(self._decoder_buffer[:2], byteorder='big')
            expected_len = length + self.overhead_length

            if len(self._decoder_buffer) < expected_len:
                break

            nonce = self._decoder_buffer[2:2 + self.nonce_length]
            ciphertext = self._decoder_buffer[2 + self.nonce_length:expected_len]

            result.append(self.cipher.decrypt(nonce, ciphertext, None))
            self._decoder_buffer = self._decoder_buffer[expected_len:]

        return result