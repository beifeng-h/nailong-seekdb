"""Minimal MySQL client (stdlib only) used by the seekdb FTS benchmark."""

import socket
import struct


class MysqlError(Exception):
    pass


class Mysql:
    CLIENT_LONG_PASSWORD = 0x00000001
    CLIENT_LONG_FLAG = 0x00000004
    CLIENT_CONNECT_WITH_DB = 0x00000008
    CLIENT_PROTOCOL_41 = 0x00000200
    CLIENT_TRANSACTIONS = 0x00002000
    CLIENT_SECURE_CONNECTION = 0x00008000
    CLIENT_PLUGIN_AUTH = 0x00080000
    CLIENT_CONNECT_ATTRS = 0x00100000

    def __init__(self, host="127.0.0.1", port=2881, user="root",
                 password="", db=None, timeout=120.0):
        self.host = host
        self.port = port
        self.user = user
        self.password = password or ""
        self.db = db
        self.timeout = timeout
        self.sock = None
        self.seq = 0
        self.buf = b""

    # ---------------------------------------------------------------- io ---
    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise MysqlError("connection closed by server")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_packet(self):
        header = self._recv_exact(4)
        length = header[0] | (header[1] << 8) | (header[2] << 16)
        self.seq = (header[3] + 1) & 0xFF
        payload = b""
        while True:
            payload += self._recv_exact(length)
            if length < 0xFFFFFF:
                break
            length = struct.unpack("<I", self._recv_exact(4))[0]
            self.seq = (self.seq + 1) & 0xFF
        return payload

    def _write_packet(self, payload):
        offset = 0
        while True:
            chunk = payload[offset:offset + 0xFFFFFF]
            header = struct.pack("<I", len(chunk))[:3] + bytes([self.seq])
            self.sock.sendall(header + chunk)
            self.seq = (self.seq + 1) & 0xFF
            offset += len(chunk)
            if len(chunk) < 0xFFFFFF:
                break

    # ----------------------------------------------------------- helpers ---
    @staticmethod
    def _lenenc_int(data, pos):
        first = data[pos]
        if first < 0xFB:
            return first, pos + 1
        if first == 0xFC:
            return struct.unpack("<H", data[pos + 1:pos + 3])[0], pos + 3
        if first == 0xFD:
            return int.from_bytes(data[pos + 1:pos + 4], "little"), pos + 4
        if first == 0xFE:
            return struct.unpack("<Q", data[pos + 1:pos + 9])[0], pos + 9
        return None, pos + 1

    @classmethod
    def _lenenc_str(cls, data, pos):
        length, pos = cls._lenenc_int(data, pos)
        if length is None:
            return None, pos
        return data[pos:pos + length], pos + length

    @staticmethod
    def _skip_cstr(data, pos):
        end = data.index(b"\x00", pos)
        return end + 1

    # ---------------------------------------------------------- handshake ---
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port),
                                             timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.seq = 0
        greeting = self._read_packet()
        if greeting[0] == 0xFF:
            raise MysqlError(self._error_text(greeting))

        pos = 1
        pos = self._skip_cstr(greeting, pos)          # server version
        pos += 4                                       # thread id
        seed = greeting[pos:pos + 8]
        pos += 8 + 1                                   # scramble part 1 + filler
        caps_lower = struct.unpack("<H", greeting[pos:pos + 2])[0]
        pos += 2
        pos += 1                                       # charset
        pos += 2                                       # status flags
        caps_upper = struct.unpack("<H", greeting[pos:pos + 2])[0]
        pos += 2
        server_caps = caps_lower | (caps_upper << 16)
        auth_len = greeting[pos]
        pos += 1
        pos += 10                                      # reserved
        seed += greeting[pos:pos + max(13, auth_len - 8)]
        pos += max(13, auth_len - 8)
        plugin = b"mysql_native_password"
        if (server_caps & self.CLIENT_PLUGIN_AUTH) and pos < len(greeting):
            end = greeting.index(b"\x00", pos)
            plugin = greeting[pos:end]

        caps = (self.CLIENT_LONG_PASSWORD | self.CLIENT_LONG_FLAG |
                self.CLIENT_PROTOCOL_41 | self.CLIENT_TRANSACTIONS |
                self.CLIENT_SECURE_CONNECTION)
        if self.db:
            caps |= self.CLIENT_CONNECT_WITH_DB
        if server_caps & self.CLIENT_PLUGIN_AUTH:
            caps |= self.CLIENT_PLUGIN_AUTH

        if plugin == b"caching_sha2_password":
            auth_response = b""
        else:
            auth_response = self._native_password(self.password, seed)

        payload = struct.pack("<IIB", caps, 16 * 1024 * 1024, 45) + b"\x00" * 23
        payload += self.user.encode() + b"\x00"
        payload += bytes([len(auth_response)]) + auth_response
        if self.db:
            payload += self.db.encode() + b"\x00"
        if caps & self.CLIENT_PLUGIN_AUTH:
            payload += plugin + b"\x00"
        self._write_packet(payload)

        while True:
            pkt = self._read_packet()
            head = pkt[0]
            if head == 0x00:
                return self
            if head == 0xFF:
                raise MysqlError(self._error_text(pkt))
            if head == 0xFE:
                continue
            # AuthMoreData: fast-auth-success marker for caching_sha2_password
            continue

    @staticmethod
    def _native_password(password, seed):
        if not password:
            return b""
        import hashlib
        stage1 = hashlib.sha1(password.encode()).digest()
        stage2 = hashlib.sha1(stage1).digest()
        token = hashlib.sha1(seed + stage2).digest()
        return bytes(a ^ b for a, b in zip(stage1, token))

    @staticmethod
    def _error_text(pkt):
        code = struct.unpack("<H", pkt[1:3])[0]
        marker = pkt.find(b"#", 3)
        if marker == -1:
            marker = 3
        try:
            state = pkt[3:marker].decode("utf-8", "replace")
        except Exception:
            state = ""
        return "errno=%s state=%s msg=%s" % (
            code, state, pkt[marker + 1:].decode("utf-8", "replace"))

    # ------------------------------------------------------------ queries ---
    def query(self, sql):
        self.seq = 0
        self._write_packet(b"\x03" + sql.encode("utf-8"))
        pkt = self._read_packet()
        head = pkt[0]
        if head == 0xFF:
            raise MysqlError(self._error_text(pkt))
        if head == 0x00 or head == 0xFE:
            return None
        col_count, _ = self._lenenc_int(pkt, 0)
        for _ in range(col_count):
            self._read_packet()
        eof = self._read_packet()
        if eof[0] == 0xFF:
            raise MysqlError(self._error_text(eof))
        rows = []
        while True:
            pkt = self._read_packet()
            if pkt[0] == 0xFF:
                raise MysqlError(self._error_text(pkt))
            if pkt[0] == 0xFE and len(pkt) < 9:
                break
            pos = 0
            values = []
            for _ in range(col_count):
                value, pos = self._lenenc_str(pkt, pos)
                values.append(value)
            rows.append(tuple(values))
        return rows

    def scalar(self, sql):
        rows = self.query(sql)
        if not rows:
            return None
        return rows[0][0]

    def close(self):
        if self.sock:
            try:
                self.seq = 0
                self._write_packet(b"\x01")
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
