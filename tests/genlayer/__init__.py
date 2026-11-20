# Offline stub of the genlayer module so the real contract file runs unchanged.
class _TM(dict):
    def __class_getitem__(cls, k): return cls
TreeMap = _TM
u256 = int
PAYOUTS = []
class Address:
    def __init__(self, h): self.as_hex = h
class _W:
    def __call__(self, f): return f
    def payable(self, f): return f
class _Public:
    write = _W()
    @staticmethod
    def view(f): return f
class _Msg:
    sender_address = Address("0xaaa")
    value = 0
class _Eq:
    @staticmethod
    def strict_eq(fn): return fn()
class _Web:
    RESP = {}
    @staticmethod
    def render(url, mode="text"):
        for k, v in _Web.RESP.items():
            if k in url: return v
        raise Exception("unreachable " + url)
class _Nondet: web = _Web
class _Proxy:
    def __init__(self, a): self.a = a
    def emit_transfer(self, value): PAYOUTS.append((self.a.as_hex, int(value)))
class _Evm:
    @staticmethod
    def contract_interface(cls): return _Proxy
class Contract:
    def __init_subclass__(cls, **kw):
        orig = cls.__init__
        anns = cls.__annotations__
        def init(self, *a, **k):
            for n, t in anns.items():
                if t is _TM: setattr(self, n, _TM())
            orig(self, *a, **k)
        cls.__init__ = init
class gl:
    Contract = Contract
    public = _Public
    message = _Msg
    message_raw = {"datetime": "2026-11-20T10:00:00Z"}
    eq_principle = _Eq
    nondet = _Nondet
    evm = _Evm
__all__ = ["gl", "TreeMap", "u256", "Address"]
