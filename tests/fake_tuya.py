"""A fake Tuya v3.3 light on localhost for tests with the real tinytuya: it acks each
command (empty cmd 7) and pushes a STATUS (cmd 8) only when a value changes."""
import json, socket, struct, threading, time
from tinytuya.core.message_helper import pack_message, unpack_message, parse_header, TuyaMessage
from tinytuya.core.crypto_helper import AESCipher

KEY = b"0123456789abcdef"

class FakeBulb:
    def __init__(self, delay=0.08, skip=None, dps=None):
        self.dps = dps or {"20": True, "21": "white", "22": 500, "23": 300,
                           "24": "000003e803e8", "25": "", "26": 0, "27": ""}
        self.delay = delay
        self.skip = skip or (lambda cmd, n, obj: False)
        self.srv = socket.socket(); self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0)); self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.log = []; self.conns = 0; self.n = 0; self.clients = []
        self.push_status = True  # False: acks only (GU10s getting colors quickly)
        threading.Thread(target=self._accept, daemon=True).start()
    def _accept(self):
        while True:
            c, _ = self.srv.accept(); self.conns += 1; self.clients.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()
    def _send(self, c, cmd, seq, obj, hdr=False):
        if obj is None:
            payload = b""
        else:
            enc = AESCipher(KEY).encrypt(json.dumps(obj).encode(), False)
            payload = (b"3.3" + b"\0" * 12 + enc) if hdr else enc
        c.sendall(pack_message(TuyaMessage(seq, cmd, 0, struct.pack(">I", 0) + payload, 0, True, 0x55AA, None)))
    def close(self):
        """Stop listening and drop the connections (no leftovers between tests)."""
        for c in self.clients + [self.srv]:
            try: c.close()
            except OSError: pass

    def push(self, changes):
        self.dps.update(changes)
        for c in self.clients:
            try: self._send(c, 8, 0, {"devId": "x", "dps": changes, "t": 1}, hdr=True)
            except OSError: pass
    def _serve(self, c):
        buf = b""
        while True:
            try:
                d = c.recv(4096)
            except OSError:
                return
            if not d: return
            buf += d
            while len(buf) >= 16:
                h = parse_header(buf)
                if len(buf) < h.total_length: break
                raw, buf = buf[:h.total_length], buf[h.total_length:]
                m = unpack_message(raw, no_retcode=True)
                p = m.payload
                if p.startswith(b"3.3"): p = p[15:]
                obj = json.loads(AESCipher(KEY).decrypt(p, False, decode_text=False)) if p else {}
                self.n += 1
                self.log.append((time.monotonic(), m.cmd, obj.get("dps")))
                sk = self.skip(m.cmd, self.n, obj)
                if sk == "apply":
                    if m.cmd == 7: self.dps.update(obj.get("dps", {}))
                    continue
                if sk:
                    continue
                time.sleep(self.delay)
                if m.cmd == 10:
                    self._send(c, 10, m.seqno, {"devId": "x", "dps": dict(self.dps)})
                elif m.cmd == 7:
                    ch = {k: v for k, v in obj.get("dps", {}).items() if self.dps.get(k) != v}
                    self.dps.update(ch)
                    self._send(c, 7, m.seqno, None)
                    if ch and self.push_status:
                        self._send(c, 8, 0, {"devId": "x", "dps": ch, "t": 1}, hdr=True)
                elif m.cmd == 9:
                    self._send(c, 9, m.seqno, None)
