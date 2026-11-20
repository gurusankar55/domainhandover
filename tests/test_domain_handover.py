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

def st(i): return json.loads(c.get_deal(i))

print("constructor floors")
live = dh.DomainHandover(False)
as_(BUYER, W)
refused(lambda: live.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 300, 7*86400, ""), "live: 5 min hold refused")
refused(lambda: live.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 86400, 300, ""), "live: 5 min retention refused")
live.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 86400, 7*86400, "")
world(rdap(registrar="1068"), dns())            # already on target
as_(SELLER); refused(lambda: live.accept_deal(0), "live: already-on-target refused at accept")
G._Web.RESP = {}
refused(lambda: live.accept_deal(0), "accept refused when registry unreadable")
G.PAYOUTS.clear()

print("happy path PUSH (test contract)")
c = dh.DomainHandover(True)
world(rdap(registrar="1068", ns=OLD), dns(ns=OLD))
as_(OTHER); c.lookup("Shop.com")
lk = json.loads(c.get_lookup("shop.com"))
ok(lk["registrar_id"] == "1068" and lk["rdap_ns"] == sorted(OLD), "lookup stored")
as_(BUYER, 0)
refused(lambda: c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 3600, 600, ""), "no value")
as_(BUYER, 5 * W)
refused(lambda: c.open_deal("shop.com", BUYER, "PUSH", "1068", TARGET, 48, 3600, 600, ""), "seller == buyer")
refused(lambda: c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 10, 600, ""), "hold too short")
refused(lambda: c.open_deal("shop.com", SELLER, "SWAP", "1068", TARGET, 48, 3600, 600, ""), "bad mode")
c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 3600, 600, "")
ok(json.loads(c.get_stats())["escrow_wei"] == str(5 * W), "escrow locked")
refused(lambda: c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 3600, 600, ""), "second active deal same domain")
as_(OTHER); refused(lambda: c.accept_deal(0), "stranger accept")
as_(SELLER); refused(lambda: c.check_handover(0), "check before accept")
c.accept_deal(0)
ok(st(0)["baseline_ns"] == sorted(OLD), "baseline recorded at accept")
c.check_handover(0)
ok(st(0)["status"] == "ACCEPTED", "NO_MATCH keeps ACCEPTED")
world(rdap(registrar="1068"), dns())
NOW[0] += 600; c.check_handover(0)
ok(st(0)["status"] == "VERIFYING", "first match starts hold")
NOW[0] += 1200; c.check_handover(0)
ok(st(0)["status"] == "VERIFYING" and G.PAYOUTS == [], "hold still running, no pay")
world(rdap(registrar="1068", ns=OLD), dns(ns=OLD))
NOW[0] += 600; c.check_handover(0)
ok(st(0)["status"] == "ACCEPTED", "revert during hold resets it")
world(rdap(registrar="1068"), dns())
NOW[0] += 60; c.check_handover(0)
NOW[0] += 3599; c.check_handover(0)
ok(st(0)["status"] == "VERIFYING", "3599s is not enough")
NOW[0] += 1; c.check_handover(0)
ok(st(0)["status"] == "RETAINED" and G.PAYOUTS == [(SELLER, 4 * W)], "80% paid, 20% retained")
as_(SELLER); refused(lambda: c.retention_check(0), "seller cannot end retention early")
as_(BUYER); c.retention_check(0)
ok(st(0)["status"] == "RETAINED", "buyer MATCH check during retention changes nothing")
NOW[0] += 600
as_(SELLER); c.retention_check(0)
ok(st(0)["status"] == "PAID" and G.PAYOUTS[-1] == (SELLER, 1 * W), "retention ended, last 20% paid")
ok(json.loads(c.get_stats())["escrow_wei"] == "0", "escrow released")
refused(lambda: c.check_handover(0), "check after paid")

print("seller moves domain back after the 80% payout")
as_(BUYER, 10 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 300, 600, "")
world(rdap(registrar="1068", ns=OLD), dns(ns=OLD))
as_(SELLER); c.accept_deal(1)
world(rdap(registrar="1068"), dns())
c.check_handover(1); NOW[0] += 300; c.check_handover(1)
ok(st(1)["status"] == "RETAINED" and G.PAYOUTS[-1] == (SELLER, 8 * W), "8 of 10 paid")
world(rdap(registrar="1068", ns=OLD), dns(ns=OLD))   # flips back
NOW[0] += 60
as_(BUYER); c.retention_check(1)
ok(st(1)["status"] == "CLAWED_BACK" and G.PAYOUTS[-1] == (BUYER, 2 * W), "buyer claws back 20% at once")

print("buyer moves to a new host (not seller's) after retention, no arbiter")
as_(BUYER, 10 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 300, 600, "")
world(rdap(registrar="1068", ns=OLD), dns(ns=OLD))
as_(SELLER); c.accept_deal(2)
world(rdap(registrar="1068"), dns())
c.check_handover(2); NOW[0] += 300; c.check_handover(2)
world(rdap(registrar="1068", ns=("x.newhost.net", "y.newhost.net")), dns(ns=("x.newhost.net", "y.newhost.net")))
as_(BUYER); c.retention_check(2)
ok(st(2)["status"] == "RETAINED", "CHANGED during retention changes nothing")
NOW[0] += 600
as_(SELLER); c.retention_check(2)
ok(st(2)["status"] == "PAID" and G.PAYOUTS[-1] == (SELLER, 2 * W), "buyer cannot steal the 20% by changing NS")

print("registry vs live DNS disagree")
as_(BUYER, 2 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 48, 300, 600, "")
world(rdap(registrar="1068", ns=OLD), dns(ns=OLD))
as_(SELLER); c.accept_deal(3)
world(rdap(registrar="1068"), dns(ns=OLD))
c.check_handover(3)
ok(st(3)["status"] == "ACCEPTED", "both sources must match")
world(rdap(registrar="9999"), dns())
c.check_handover(3)
ok("registrar is 9999" in st(3)["checks"][-1]["reasons"][0], "wrong registrar refused")

print("sources down")
G._Web.RESP = {}
c.check_handover(3)
ok(st(3)["checks"][-1]["verdict"] == "UNREADABLE", "unreadable changes nothing")

print("deadline refund")
as_(BUYER); refused(lambda: c.refund_after_deadline(3), "refund before deadline")
NOW[0] += 48 * 3600
as_(SELLER); refused(lambda: c.check_handover(3), "check after deadline")
as_(OTHER); refused(lambda: c.refund_after_deadline(3), "stranger refund")
as_(BUYER); c.refund_after_deadline(3)
ok(st(3)["status"] == "REFUNDED" and G.PAYOUTS[-1] == (BUYER, 2 * W), "buyer refunded")

print("TRANSFER mode: fresh transfer event, revert to old registrar, arbiter")
as_(BUYER, 5 * W); c.open_deal("shop.com", SELLER, "TRANSFER", "1068", TARGET, 240, 300, 600, ARB)
world(rdap(registrar="146", ns=OLD), dns(ns=OLD))
as_(SELLER); c.accept_deal(4)
world(rdap(registrar="1068", transfer="2024-01-01T00:00:00Z"), dns())
c.check_handover(4)
ok(st(4)["status"] == "ACCEPTED", "old transfer event not accepted")
world(rdap(registrar="1068", transfer="2026-11-25T09:00:00Z"), dns())
NOW[0] += 60; c.check_handover(4)
ok(st(4)["status"] == "VERIFYING", "fresh transfer event accepted")
as_(SELLER); refused(lambda: c.dispute(4, "x"), "seller cannot dispute")
as_(BUYER); c.dispute(4, "I still cannot log in to the zone")
as_(SELLER); refused(lambda: c.check_handover(4), "no checks while disputed")
refused(lambda: c.arbiter_decide(4, True), "seller cannot arbitrate")
refused(lambda: c.settle_stale_dispute(4), "arbiter still has time")
as_(ARB); c.arbiter_decide(4, False)
ok(st(4)["status"] == "REFUNDED" and G.PAYOUTS[-1] == (BUYER, 5 * W), "arbiter refund")

as_(BUYER, 5 * W); c.open_deal("shop.com", SELLER, "TRANSFER", "1068", TARGET, 240, 300, 600, ARB)
world(rdap(registrar="146", ns=OLD), dns(ns=OLD))
as_(SELLER); c.accept_deal(5)
world(rdap(registrar="1068", transfer="2026-11-26T09:00:00Z"), dns())
NOW[0] += 60; c.check_handover(5); NOW[0] += 300; c.check_handover(5)
ok(st(5)["status"] == "RETAINED", "transfer deal in retention")
world(rdap(registrar="146", ns=("p.other.com",)), dns(ns=("p.other.com",)))   # back at seller's registrar
as_(BUYER); c.retention_check(5)
ok(st(5)["status"] == "CLAWED_BACK" and G.PAYOUTS[-1] == (BUYER, 1 * W), "back at original registrar = revert")

as_(BUYER, 5 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 240, 300, 600, ARB)
world(rdap(registrar="1068", ns=OLD), dns(ns=OLD))
as_(SELLER); c.accept_deal(6)
world(rdap(registrar="1068"), dns())
c.check_handover(6); NOW[0] += 300; c.check_handover(6)
world(rdap(registrar="1068", ns=("q.other.com",)), dns(ns=("q.other.com",)))
NOW[0] += 600; c.retention_check(6)
ok(st(6)["status"] == "RETENTION_DISPUTED", "CHANGED with arbiter goes to arbiter")
as_(ARB); c.arbiter_decide(6, False)
ok(st(6)["status"] == "CLAWED_BACK" and G.PAYOUTS[-1] == (BUYER, 1 * W), "arbiter returns only the retained part")

print("cancel paths")
as_(BUYER, 1 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 24, 300, 600, "")
as_(BUYER); c.cancel_deal(7)
ok(st(7)["status"] == "REFUNDED", "buyer cancels before accept")
as_(BUYER, 1 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 24, 300, 600, "")
world(rdap(registrar="1068", ns=OLD), dns(ns=OLD))
as_(SELLER); c.accept_deal(8)
as_(BUYER); refused(lambda: c.cancel_deal(8), "buyer cannot cancel after accept")
refused(lambda: c.dispute(8, "x"), "no arbiter, no dispute")
as_(SELLER); c.cancel_deal(8)
ok(st(8)["status"] == "REFUNDED", "seller declines -> refund")
as_(BUYER, 1 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 24, 300, 600, "")
as_(BUYER); c.confirm_received(9)
ok(st(9)["status"] == "PAID", "buyer confirms early")

print("test mode allows a deal already on target (Studio demo)")
as_(BUYER, 1 * W); c.open_deal("shop.com", SELLER, "PUSH", "1068", TARGET, 24, 300, 300, "")
world(rdap(registrar="1068"), dns())
as_(SELLER); c.accept_deal(10)
c.check_handover(10); NOW[0] += 300; c.check_handover(10); NOW[0] += 300; c.retention_check(10)
ok(st(10)["status"] == "PAID", "demo path completes")

s = json.loads(c.get_stats())
total_in = (5 + 10 + 10 + 2 + 5 + 5 + 5 + 1 + 1 + 1 + 1) * W
ok(int(s["paid_wei"]) + int(s["refunded_wei"]) == total_in and s["escrow_wei"] == "0", "money conserved")
ok(sum(v for _, v in G.PAYOUTS) == total_in, "payouts sum matches deposits")
print("\nALL", passed[0], "TESTS PASSED")
