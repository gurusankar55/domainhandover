# Offline tests: python3 tests/test_domain_handover.py
import sys, os, json, importlib.util
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import genlayer as G
spec = importlib.util.spec_from_file_location("dh", os.path.join(HERE, "..", "domain_handover.py"))
dh = importlib.util.module_from_spec(spec); spec.loader.exec_module(dh)

T0 = dh.iso_to_epoch("2026-11-20T10:00:00Z")
NOW = [T0]
class FDT:
    @staticmethod
    def now(tz):
        class X:
            def timestamp(s): return NOW[0]
        return X()
dh.datetime = FDT
W = 10 ** 18
BUYER, SELLER, ARB, OTHER = "0x" + "b" * 40, "0x" + "5" * 40, "0x" + "a" * 40, "0x" + "c" * 40
passed = [0]

def ok(cond, name):
    if not cond:
        print("FAIL:", name); sys.exit(1)
    passed[0] += 1; print("  ok", name)

def as_(addr, value=0):
    G.gl.message.sender_address = G.Address(addr); G.gl.message.value = value

def refused(fn, name):
    try: fn()
    except Exception as e:
        passed[0] += 1; print("  ok refused:", name, "->", str(e)[:70]); return
    print("FAIL not refused:", name); sys.exit(1)

def rdap(registrar="1068", ns=("ada.ns.cloudflare.com", "bob.ns.cloudflare.com"), transfer=None):
    events = [{"eventAction": "registration", "eventDate": "2015-03-01T00:00:00Z"},
              {"eventAction": "last changed", "eventDate": "2026-11-19T00:00:00Z"}]
    if transfer: events.append({"eventAction": "transfer", "eventDate": transfer})
    return json.dumps({"objectClassName": "domain",
        "nameservers": [{"objectClassName": "nameserver", "ldhName": n.upper()} for n in ns],
        "entities": [{"objectClassName": "entity", "roles": ["registrar"],
                      "publicIds": [{"type": "IANA Registrar ID", "identifier": registrar}]}],
        "events": events})

def dns(ns=("ada.ns.cloudflare.com", "bob.ns.cloudflare.com"), status=0):
    return json.dumps({"Status": status, "Answer": [
        {"name": "shop.com.", "type": 2, "TTL": 21599, "data": n + "."} for n in ns]})

def world(r, d):
    G._Web.RESP = {"rdap.verisign.com": r, "dns.google": d}

TARGET = "ADA.ns.cloudflare.com., bob.ns.cloudflare.com"
OLD = ("ns1.sellerhost.com", "ns2.sellerhost.com")

print("parsers")
p = dh.parse_rdap(json.loads(rdap(transfer="2026-11-21T08:00:00Z")))
ok(p["registrar_id"] == "1068" and p["ns"] == ["ada.ns.cloudflare.com", "bob.ns.cloudflare.com"], "rdap parsed")
ok(p["last_transfer"] == dh.iso_to_epoch("2026-11-21T08:00:00Z"), "transfer event parsed")
ok(dh.parse_dns(json.loads(dns(status=3))) == [], "NXDOMAIN gives no NS")
ok(dh.clean_ns_list(TARGET) == ["ada.ns.cloudflare.com", "bob.ns.cloudflare.com"], "ns cleaned/sorted")
refused(lambda: dh.clean_domain("https://shop.com"), "url instead of domain")
refused(lambda: dh.clean_domain("a.shop.com"), "subdomain")
refused(lambda: dh.clean_domain("shop.xyz"), "unsupported tld")
refused(lambda: dh.clean_registrar_id("Cloudflare"), "non numeric registrar")

print("happy path PUSH")
c = dh.DomainHandover()
world(rdap(registrar="1068", ns=OLD), dns(ns=OLD))
as_(OTHER); c.lookup("Shop.com")
lk = json.loads(c.get_lookup("shop.com"))
ok(lk["registrar_id"] == "1068" and lk["rdap_ns"] == sorted(OLD), "lookup stored")
as_(BUYER, 0)
refused(lambda: c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 3600, ""), "no value")
as_(BUYER, 5 * W)
refused(lambda: c.open_deal("shop.com", BUYER, "PUSH", "1068", TARGET, 48, 3600, ""), "seller == buyer")
refused(lambda: c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 10, ""), "hold too short")
refused(lambda: c.open_deal("shop.com", SELLER, "SWAP", "1068", TARGET, 48, 3600, ""), "bad mode")
c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 3600, "")
ok(json.loads(c.get_stats())["escrow_wei"] == str(5 * W), "escrow locked")
refused(lambda: c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 3600, ""), "second active deal same domain")
as_(OTHER); refused(lambda: c.accept_deal(0), "stranger accept")
as_(SELLER); refused(lambda: c.check_handover(0), "check before accept")
c.accept_deal(0)
c.check_handover(0)
ok(json.loads(c.get_deal(0))["status"] == "ACCEPTED", "NO_MATCH keeps ACCEPTED")
world(rdap(registrar="1068"), dns())          # seller points NS to buyer's pair
NOW[0] += 600; c.check_handover(0)
ok(json.loads(c.get_deal(0))["status"] == "VERIFYING", "first match starts hold")
NOW[0] += 1200; c.check_handover(0)
ok(json.loads(c.get_deal(0))["status"] == "VERIFYING" and G.PAYOUTS == [], "hold still running, no pay")
world(rdap(registrar="1068", ns=OLD), dns(ns=OLD))   # seller flips back
NOW[0] += 600; c.check_handover(0)
ok(json.loads(c.get_deal(0))["status"] == "ACCEPTED", "revert resets the hold")
world(rdap(registrar="1068"), dns())
NOW[0] += 60; c.check_handover(0)
NOW[0] += 3599; c.check_handover(0)
ok(json.loads(c.get_deal(0))["status"] == "VERIFYING", "3599s is not enough")
NOW[0] += 1; c.check_handover(0)
d = json.loads(c.get_deal(0))
ok(d["status"] == "PAID" and G.PAYOUTS == [(SELLER, 5 * W)], "seller paid after full hold")
ok(json.loads(c.get_stats())["escrow_wei"] == "0", "escrow released")
refused(lambda: c.check_handover(0), "check after paid")

print("registry vs live DNS disagree")
as_(BUYER, 2 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 300, "")
as_(SELLER); c.accept_deal(1)
world(rdap(registrar="1068"), dns(ns=OLD))     # registry updated, DNS still old
c.check_handover(1)
ok(json.loads(c.get_deal(1))["status"] == "ACCEPTED", "both sources must match")
world(rdap(registrar="9999"), dns())
c.check_handover(1)
ok("registrar is 9999" in json.loads(c.get_deal(1))["checks"][-1]["reasons"][0], "wrong registrar refused")

print("sources down")
G._Web.RESP = {}
c.check_handover(1)
ok(json.loads(c.get_deal(1))["checks"][-1]["verdict"] == "UNREADABLE", "unreadable changes nothing")

print("deadline refund")
as_(BUYER); refused(lambda: c.refund_after_deadline(1), "refund before deadline")
NOW[0] += 48 * 3600
as_(SELLER); refused(lambda: c.check_handover(1), "check after deadline")
as_(OTHER); refused(lambda: c.refund_after_deadline(1), "stranger refund")
as_(BUYER); c.refund_after_deadline(1)
ok(json.loads(c.get_deal(1))["status"] == "REFUNDED" and G.PAYOUTS[-1] == (BUYER, 2 * W), "buyer refunded")

print("TRANSFER mode needs a fresh transfer event")
as_(BUYER, 3 * W); c.open_deal("shop.com", SELLER, "TRANSFER", "1068", TARGET, 240, 300, ARB)
as_(SELLER); c.accept_deal(2)
acc = json.loads(c.get_deal(2))["accepted_at"]
world(rdap(registrar="1068", transfer="2024-01-01T00:00:00Z"), dns())
c.check_handover(2)
ok(json.loads(c.get_deal(2))["status"] == "ACCEPTED", "old transfer event not accepted")
world(rdap(registrar="1068", transfer="2026-11-23T09:00:00Z"), dns())
NOW[0] += 60; c.check_handover(2)
ok(json.loads(c.get_deal(2))["status"] == "VERIFYING", "fresh transfer event accepted")

print("dispute + arbiter")
as_(SELLER); refused(lambda: c.dispute(2, "x"), "seller cannot dispute")
as_(BUYER); c.dispute(2, "I still cannot log in to the zone")
as_(SELLER); refused(lambda: c.check_handover(2), "no checks while disputed")
refused(lambda: c.arbiter_decide(2, True), "seller cannot arbitrate")
refused(lambda: c.settle_stale_dispute(2), "arbiter still has time")
as_(ARB); c.arbiter_decide(2, False)
ok(json.loads(c.get_deal(2))["status"] == "REFUNDED" and G.PAYOUTS[-1] == (BUYER, 3 * W), "arbiter refund")

print("cancel paths")
as_(BUYER, 1 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 24, 300, "")
as_(BUYER); c.cancel_deal(3)
ok(json.loads(c.get_deal(3))["status"] == "REFUNDED", "buyer cancels before accept")
as_(BUYER, 1 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 24, 300, "")
as_(SELLER); c.accept_deal(4)
as_(BUYER); refused(lambda: c.cancel_deal(4), "buyer cannot cancel after accept")
refused(lambda: c.dispute(4, "x"), "no arbiter, no dispute")
as_(SELLER); c.cancel_deal(4)
ok(json.loads(c.get_deal(4))["status"] == "REFUNDED", "seller declines -> refund")
as_(BUYER, 1 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 24, 300, "")
as_(BUYER); c.confirm_received(5)
ok(json.loads(c.get_deal(5))["status"] == "PAID", "buyer confirms early")

s = json.loads(c.get_stats())
total_in = (5 + 2 + 3 + 1 + 1 + 1) * W
ok(int(s["paid_wei"]) + int(s["refunded_wei"]) == total_in and s["escrow_wei"] == "0", "money conserved")
ok(sum(v for _, v in G.PAYOUTS) == total_in, "payouts sum matches deposits")
print("\nALL", passed[0], "TESTS PASSED")
