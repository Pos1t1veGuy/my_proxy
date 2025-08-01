import json
import hashlib
import os
import logging
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

from ..proxy_server import Socks5Server
from ..ciphers import *
from ..wrappers import HTTP_WS_Wrapper


users_file = Path(__file__).parent / "users.json"
key = b'keykey'
log_file = Path(__file__).parent / 'logs' / "proxy.log"


os.makedirs(log_file.parent, exist_ok=True)
os.makedirs(users_file.parent, exist_ok=True)

file_handler = TimedRotatingFileHandler(
    log_file,
    when="midnight",
    interval=1,
    backupCount=30,
    encoding="utf-8"
)
formatter = logging.Formatter(
    fmt="[{asctime}] [{levelname}] {message}",
    datefmt="%Y-%m-%d %H:%M:%S",
    style="{"
)
file_handler.setFormatter(formatter)
users = json.load(open(users_file, 'r', encoding='utf-8'))


key = b'\x86P\x0e\xd3\xd4\xf2\xbc\x19\x1f\x98\xc5\xd0e\xf3X\x07\xf7\xd5R_\x9b\x1c\x92R\xe0}JY\x94\x01nF'
hash_key = hashlib.sha256(key).digest()
available_ciphers = [
    Cipher(wrapper=HTTP_WS_Wrapper()), # starts a handshake with client_hello and server_hello from wrapper
    AES_CBC(key=hash_key, iv=os.urandom(16)),
    AES_CTR(key=hash_key, iv=os.urandom(16)),
    ChaCha20_Poly1305(key=key),
]
SERVER = Socks5Server(
    users=users['users'],
    accept_anonymous=users['accept_anonymous'],
    ciphers=available_ciphers,
    udp_cipher=available_ciphers[2],
    port=180
)
# SERVER.logger.addHandler(file_handler)
asyncio.run(SERVER.start())