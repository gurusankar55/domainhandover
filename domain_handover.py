# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *

# DomainHandover - trustless escrow for selling a .com / .net domain.
#
# The buyer locks GEN in a deal and declares, up front, where the domain must
# end up: the target registrar (IANA Registrar ID) and the target nameservers
# (for example the Cloudflare pair assigned to the buyer's account).
#
# GenLayer validators each read two independent public sources themselves:
#   1. the registry's RDAP record (Verisign, authoritative for .com/.net)
#   2. live DNS delegation through Google DNS-over-HTTPS
# They reduce what they read to one canonical verdict and must agree on it
# exactly (strict_eq). Raw JSON differs between reads (TTLs, timestamps),
# the canonical verdict does not.
#
# A handover is only paid out after TWO matching checks at least
# `hold_seconds` apart, so a seller cannot flip the nameservers, get paid and
# flip them back. Any failing check during the hold resets the deal.
# After the hold, 80% goes to the seller and 20% stays locked for a retention
# period. If validators see the domain back on the seller's ORIGINAL setup
# (recorded at acceptance), the retained 20% returns to the buyer.
# If the seller never delivers, the buyer is refunded after the deadline.
# Every deal is its own escrow. There is no shared pool, so a test contract
# can never touch anyone else's money.

from datetime import datetime, timezone
import json

WEI = 10 ** 18
MIN_PRICE_WEI = WEI // 1000          # 0.001 GEN
# Floors depend on how the contract was deployed (see __init__).
LIVE_MIN_HOLD = 86400                # live contract: hold at least 24 h
LIVE_MIN_RETENTION = 7 * 86400       # live contract: retention at least 7 days
TEST_MIN_HOLD = 300                  # test contract only: 5 min, for Studio demos
TEST_MIN_RETENTION = 300
MAX_HOLD_SECONDS = 7 * 86400
MAX_RETENTION_SECONDS = 90 * 86400
RETAIN_BPS = 2000                    # 20% of the price stays locked during retention
MIN_DEADLINE_HOURS = 1
MAX_DEADLINE_HOURS = 60 * 24         # 60 days (inter-registrar transfers can take ~7)
VERIFY_GRACE_SECONDS = 86400         # time to finish a hold that started before the deadline
ARBITER_TIMEOUT_SECONDS = 14 * 86400
MAX_NS = 4

RDAP_BASE = {
    "com": "https://rdap.verisign.com/com/v1/domain/",
    "net": "https://rdap.verisign.com/net/v1/domain/",
}
DNS_BASE = "https://dns.google/resolve?type=NS&name="

MODES = ["PUSH", "TRANSFER"]
# PUSH     = account change inside the same registrar (no RDAP transfer event).
# TRANSFER = inter-registrar transfer; RDAP must also show a "transfer" event
#            dated after the seller accepted the deal.

# Verdicts produced by the consensus check
MATCH = "MATCH"
NO_MATCH = "NO_MATCH"
UNREADABLE = "UNREADABLE"

REVERTED = "REVERTED"      # retention check: back on the seller's original setup
CHANGED = "CHANGED"        # retention check: neither target nor original

# Deal statuses
OPEN = "OPEN"                      # funded by buyer, waiting for seller to accept
ACCEPTED = "ACCEPTED"              # seller agreed, handover in progress
VERIFYING = "VERIFYING"            # first matching check passed, hold running
DISPUTED = "DISPUTED"              # buyer disputed during hold, arbiter decides
RETAINED = "RETAINED"              # 80% paid, 20% locked until retention ends
RETENTION_DISPUTED = "RETENTION_DISPUTED"  # retention check was CHANGED, arbiter decides
PAID = "PAID"                      # closed, seller got everything still owed
REFUNDED = "REFUNDED"              # closed, buyer got the full price back
CLAWED_BACK = "CLAWED_BACK"        # closed, 80% to seller, retained 20% back to buyer


# ---------------- payouts ----------------

@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


def pay(to_hex: str, amount: int) -> None:
    if amount > 0:
        _Recipient(Address(to_hex)).emit_transfer(value=u256(amount))


# ---------------- time ----------------

def _days_from_civil(y: int, m: int, d: int) -> int:
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    mp = (m + 9) % 12
    doy = (153 * mp + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def iso_to_epoch(s: str) -> int:
    # Accepts "2026-11-20T10:15:30Z", "2026-11-20T10:15:30.123+00:00", "2026-11-20 10:15:30"
    t = str(s).strip().replace("T", " ")
    if len(t) < 16 or t[4] != "-" or t[7] != "-" or t[13] != ":":
        raise Exception("bad datetime: " + str(s))
    y, mo, d = int(t[0:4]), int(t[5:7]), int(t[8:10])
    hh, mi = int(t[11:13]), int(t[14:16])
    ss = 0
    if len(t) >= 19 and t[16] == ":":
        ss = int(t[17:19])
    return _days_from_civil(y, mo, d) * 86400 + hh * 3600 + mi * 60 + ss


def now_s() -> int:
    # GenVM time is pinned to the transaction, identical for every validator.
    try:
        return int(datetime.now(timezone.utc).timestamp())
    except Exception:
        return iso_to_epoch(str(gl.message_raw["datetime"]))


# ---------------- input cleaning (deterministic) ----------------

def _is_label(s: str) -> bool:
    if len(s) < 1 or len(s) > 63:
        return False
    if s[0] == "-" or s[-1] == "-":
        return False
    for ch in s:
        if not (("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "-"):
            return False
    return True


def clean_domain(raw: str) -> str:
    d = str(raw).strip().lower()
    if d.startswith("http://") or d.startswith("https://") or "/" in d:
        raise Exception("Give only the domain, like example.com")
    if d.endswith("."):
        d = d[:-1]
    parts = d.split(".")
    if len(parts) != 2:
        raise Exception("Only a registrable domain like example.com (no subdomains)")
    if parts[1] not in RDAP_BASE:
        raise Exception("Supported endings: .com and .net")
    if not _is_label(parts[0]):
        raise Exception("Domain name has invalid characters")
    return d


def clean_host(raw: str) -> str:
    h = str(raw).strip().lower()
    if h.endswith("."):
        h = h[:-1]
    parts = h.split(".")
    if len(parts) < 2:
        raise Exception("Bad nameserver: " + str(raw))
    for p in parts:
        if not _is_label(p):
            raise Exception("Bad nameserver: " + str(raw))
    return h


def clean_ns_list(raw: str) -> list:
    items = [x for x in str(raw).replace(" ", ",").split(",") if x.strip() != ""]
    if len(items) < 1 or len(items) > MAX_NS:
        raise Exception("Give 1 to " + str(MAX_NS) + " nameservers, comma separated")
    out = sorted(set([clean_host(x) for x in items]))
    return out


def clean_registrar_id(raw: str) -> str:
    r = str(raw).strip()
    if r == "" or len(r) > 8 or not r.isdigit():
        raise Exception("Registrar ID must be the numeric IANA Registrar ID, like 1068")
    return str(int(r))


# ---------------- reading public sources (deterministic parsing) ----------------

def _json_from_text(text: str) -> dict:
    a = text.find("{")
    b = text.rfind("}")
    if a < 0 or b <= a:
        raise Exception("no json")
    return json.loads(text[a:b + 1])


def parse_rdap(doc: dict) -> dict:
    # Pull only stable fields out of a registry RDAP record.
    ns = []
    for n in doc.get("nameservers", []) or []:
        name = str(n.get("ldhName", "")).strip().lower().rstrip(".")
        if name != "":
            ns.append(name)
    registrar_id = ""
    for e in doc.get("entities", []) or []:
        roles = e.get("roles", []) or []
        if "registrar" in roles:
            for pid in e.get("publicIds", []) or []:
                if "iana" in str(pid.get("type", "")).lower():
                    registrar_id = str(pid.get("identifier", "")).strip()
    last_transfer = 0
    for ev in doc.get("events", []) or []:
        if str(ev.get("eventAction", "")).lower() == "transfer":
            try:
                t = iso_to_epoch(str(ev.get("eventDate", "")))
                if t > last_transfer:
                    last_transfer = t
            except Exception:
                pass
    if registrar_id.isdigit():
        registrar_id = str(int(registrar_id))
    return {"registrar_id": registrar_id, "ns": sorted(set(ns)), "last_transfer": last_transfer}


def parse_dns(doc: dict) -> list:
    # Google DNS JSON: Status 0 = NOERROR, NS records are type 2.
    if int(doc.get("Status", -1)) != 0:
        return []
    out = []
    for a in doc.get("Answer", []) or []:
        if int(a.get("type", 0)) == 2:
            out.append(str(a.get("data", "")).strip().lower().rstrip("."))
    return sorted(set([x for x in out if x != ""]))


def read_state(domain: str) -> dict:
    # Runs inside the non-deterministic block on every validator.
    tld = domain.split(".")[1]
    rdap_text = gl.nondet.web.render(RDAP_BASE[tld] + domain, mode="text")
    rdap = parse_rdap(_json_from_text(rdap_text))
    dns_text = gl.nondet.web.render(DNS_BASE + domain, mode="text")
    dns_ns = parse_dns(_json_from_text(dns_text))
    return {"registrar_id": rdap["registrar_id"], "rdap_ns": rdap["ns"],
            "dns_ns": dns_ns, "last_transfer": rdap["last_transfer"]}


def decide(state: dict, mode: str, target_registrar: str, target_ns: list, accepted_at: int) -> dict:
    # Pure function: same state + same deal terms -> same verdict on every validator.
    if state["registrar_id"] == "" or len(state["rdap_ns"]) == 0:
        return {"verdict": UNREADABLE, "reasons": ["registry record unreadable"]}
    reasons = []
    if state["registrar_id"] != target_registrar:
        reasons.append("registrar is " + state["registrar_id"] + ", expected " + target_registrar)
    if state["rdap_ns"] != target_ns:
        reasons.append("registry nameservers do not match target")
    if state["dns_ns"] != target_ns:
        reasons.append("live DNS nameservers do not match target")
    if mode == "TRANSFER" and int(state["last_transfer"]) <= int(accepted_at):
        reasons.append("no registrar transfer recorded after the deal was accepted")
    return {"verdict": MATCH if len(reasons) == 0 else NO_MATCH, "reasons": reasons}


def classify_retention(state: dict, mode: str, target_registrar: str, target_ns: list,
                       baseline_registrar: str, baseline_ns: list) -> str:
    # Pure function used after the 80% payout.
    # MATCH    = still on the buyer's target registrar + nameservers
    # REVERTED = back on the seller's original nameservers (or, for TRANSFER
    #            mode, back at the seller's original registrar)
    # CHANGED  = something else (cannot be blamed on either side automatically)
    if state["registrar_id"] == "" or len(state["rdap_ns"]) == 0:
        return UNREADABLE
    if state["registrar_id"] == target_registrar and state["rdap_ns"] == target_ns \
            and state["dns_ns"] == target_ns:
        return MATCH
    if state["rdap_ns"] == baseline_ns:
        return REVERTED
    if mode == "TRANSFER" and state["registrar_id"] == baseline_registrar:
        return REVERTED
    return CHANGED


# ---------------- contract ----------------

class DomainHandover(gl.Contract):
    test_mode: bool               # True only on a contract deployed for demos
    min_hold: u256
    min_retention: u256
    next_id: u256
    deals: TreeMap[str, str]      # deal id -> deal JSON
    lookups: TreeMap[str, str]    # domain -> last consensus snapshot JSON
    active: TreeMap[str, str]     # domain -> id of its one active deal ("" when none)
    escrow_total: u256            # GEN (wei) currently locked across all deals
    paid_total: u256
    refunded_total: u256

    def __init__(self, test_mode: bool):
        self.test_mode = bool(test_mode)
        self.min_hold = TEST_MIN_HOLD if self.test_mode else LIVE_MIN_HOLD
        self.min_retention = TEST_MIN_RETENTION if self.test_mode else LIVE_MIN_RETENTION
        self.next_id = 0
        self.escrow_total = 0
        self.paid_total = 0
        self.refunded_total = 0

    # ---------- helpers ----------

    def _sender(self) -> str:
        return gl.message.sender_address.as_hex.lower()

    def _deal(self, deal_id: int) -> dict:
        key = str(int(deal_id))
        if key not in self.deals:
            raise Exception("No such deal")
        return json.loads(self.deals[key])

    def _put(self, d: dict) -> None:
        self.deals[str(d["id"])] = json.dumps(d, sort_keys=True)

    def _log(self, d: dict, event: str, now: int) -> None:
        d["history"].append({"at": now, "event": event})
        if len(d["history"]) > 40:
            d["history"] = d["history"][-40:]

    def _record_check(self, d: dict, now: int, kind: str, verdict: str, reasons: list, st: dict) -> None:
        d["checks"].append({"at": now, "kind": kind, "verdict": verdict, "reasons": reasons,
                            "registrar_id": st["registrar_id"], "rdap_ns": st["rdap_ns"],
                            "dns_ns": st["dns_ns"]})
        if len(d["checks"]) > 20:
            d["checks"] = d["checks"][-20:]

    def _consensus_state(self, domain: str) -> dict:
        def fetch() -> str:
            try:
                return json.dumps(read_state(domain), sort_keys=True)
            except Exception:
                return json.dumps({"registrar_id": "", "rdap_ns": [], "dns_ns": [],
                                   "last_transfer": 0}, sort_keys=True)
        return json.loads(gl.eq_principle.strict_eq(fetch))

    def _locked(self, d: dict) -> int:
        return int(d["price_wei"]) - int(d["paid_wei"]) - int(d["refunded_wei"])

    def _close(self, d: dict, status: str, now: int) -> None:
        d["status"] = status
        d["closed_at"] = now
        self.active[d["domain"]] = ""

    def _pay_seller(self, d: dict, amount: int, now: int, why: str) -> None:
        if amount <= 0:
            return
        d["paid_wei"] = str(int(d["paid_wei"]) + amount)
        self.escrow_total = int(self.escrow_total) - amount
        self.paid_total = int(self.paid_total) + amount
        self._log(d, "PAID " + str(amount) + " wei to seller: " + why, now)
        pay(d["seller"], amount)

    def _pay_buyer(self, d: dict, amount: int, now: int, why: str) -> None:
        if amount <= 0:
            return
        d["refunded_wei"] = str(int(d["refunded_wei"]) + amount)
        self.escrow_total = int(self.escrow_total) - amount
        self.refunded_total = int(self.refunded_total) + amount
        self._log(d, "REFUNDED " + str(amount) + " wei to buyer: " + why, now)
        pay(d["buyer"], amount)

    def _release_all(self, d: dict, now: int, why: str) -> None:
        amount = self._locked(d)
        self._close(d, PAID, now)
        self._put(d)
        self._pay_seller(d, amount, now, why)
        self._put(d)

    def _refund_all(self, d: dict, now: int, why: str) -> None:
        amount = self._locked(d)
        self._close(d, REFUNDED, now)
        self._put(d)
        self._pay_buyer(d, amount, now, why)
        self._put(d)

    def _hold_complete(self, d: dict, now: int, why: str) -> None:
        # Hold passed: pay 80% now, keep 20% for the retention period.
        price = int(d["price_wei"])
        retained = price * RETAIN_BPS // 10000
        first = price - retained
        d["status"] = RETAINED
        d["retained_wei"] = str(retained)
        d["retained_until"] = now + int(d["retention_seconds"])
        self._put(d)
        self._pay_seller(d, first, now, why + " (retention started)")
        self._put(d)

    # ---------- public lookup (helps buyers fill in a deal) ----------

    @gl.public.write
    def lookup(self, domain: str) -> None:
        # Stores the current registrar ID and nameservers, agreed by validators.
        dom = clean_domain(domain)
        st = self._consensus_state(dom)
        st["domain"] = dom
        st["checked_at"] = now_s()
        self.lookups[dom] = json.dumps(st, sort_keys=True)

    # ---------- deal lifecycle ----------

    @gl.public.write.payable
    def open_deal(self, domain: str, seller: str, mode: str, target_registrar_id: str,
                  target_nameservers: str, deadline_hours: int, hold_seconds: int,
                  retention_seconds: int, arbiter: str) -> None:
        price = int(gl.message.value)
        if price < MIN_PRICE_WEI:
            raise Exception("Send the deal price (at least 0.001 GEN) as value")
        dom = clean_domain(domain)
        m = str(mode).strip().upper()
        if m not in MODES:
            raise Exception("mode must be PUSH or TRANSFER")
        buyer = self._sender()
        sel = str(seller).strip().lower()
        if not sel.startswith("0x") or len(sel) != 42:
            raise Exception("seller must be a 0x wallet address")
        if sel == buyer:
            raise Exception("seller and buyer must be different")
        arb = str(arbiter).strip().lower()
        if arb in ("", "0", "none", "0x0000000000000000000000000000000000000000"):
            arb = ""
        elif not arb.startswith("0x") or len(arb) != 42 or arb in (buyer, sel):
            raise Exception("arbiter must be empty or a third 0x address")
        dh = int(deadline_hours)
        if dh < MIN_DEADLINE_HOURS or dh > MAX_DEADLINE_HOURS:
            raise Exception("deadline_hours must be " + str(MIN_DEADLINE_HOURS) + ".." + str(MAX_DEADLINE_HOURS))
        hs = int(hold_seconds)
        if hs < int(self.min_hold) or hs > MAX_HOLD_SECONDS:
            raise Exception("hold_seconds must be " + str(int(self.min_hold)) + ".." + str(MAX_HOLD_SECONDS))
        rs = int(retention_seconds)
        if rs < int(self.min_retention) or rs > MAX_RETENTION_SECONDS:
            raise Exception("retention_seconds must be " + str(int(self.min_retention)) + ".." + str(MAX_RETENTION_SECONDS))
        if dom in self.active and self.active[dom] != "":
            raise Exception("This domain already has an active deal (#" + self.active[dom] + ")")

        now = now_s()
        did = int(self.next_id)
        d = {
            "id": did,
            "domain": dom,
            "buyer": buyer,
            "seller": sel,
            "arbiter": arb,
            "mode": m,
            "target_registrar_id": clean_registrar_id(target_registrar_id),
            "target_ns": clean_ns_list(target_nameservers),
            "baseline_registrar_id": "",
            "baseline_ns": [],
            "price_wei": str(price),
            "paid_wei": "0",
            "refunded_wei": "0",
            "retained_wei": "0",
            "created_at": now,
            "deadline": now + dh * 3600,
            "hold_seconds": hs,
            "retention_seconds": rs,
            "accepted_at": 0,
            "first_match_at": 0,
            "retained_until": 0,
            "disputed_at": 0,
            "closed_at": 0,
            "status": OPEN,
            "checks": [],
            "history": [],
        }
        self._log(d, "OPEN by buyer", now)
        self.next_id = did + 1
        self.escrow_total = int(self.escrow_total) + price
        self.active[dom] = str(did)
        self._put(d)

    @gl.public.write
    def accept_deal(self, deal_id: int) -> None:
        # Validators record the seller's ORIGINAL setup (baseline). It is used
        # later to detect a seller who moves the domain back after payout.
        d = self._deal(deal_id)
        if self._sender() != d["seller"]:
            raise Exception("Only the seller can accept")
        if d["status"] != OPEN:
            raise Exception("Deal is not OPEN")
        now = now_s()
        if now >= int(d["deadline"]):
            raise Exception("Deadline passed")
        st = self._consensus_state(d["domain"])
        if st["registrar_id"] == "" or len(st["rdap_ns"]) == 0:
            raise Exception("Registry record unreadable right now; try accepting again later")
        already = decide(st, "PUSH", d["target_registrar_id"], d["target_ns"], 0)["verdict"] == MATCH
        if already and not bool(self.test_mode):
            raise Exception("Domain is already on the target setup; there is nothing to hand over")
        d["baseline_registrar_id"] = st["registrar_id"]
        d["baseline_ns"] = st["rdap_ns"]
        d["status"] = ACCEPTED
        d["accepted_at"] = now
        self._record_check(d, now, "baseline", "BASELINE", [], st)
        self._log(d, "ACCEPTED by seller" + (" (test mode: already on target)" if already else ""), now)
        self._put(d)

    @gl.public.write
    def cancel_deal(self, deal_id: int) -> None:
        # Buyer may cancel before the seller accepts. Seller may decline any time
        # before the first payout (this always returns money to the buyer).
        d = self._deal(deal_id)
        who = self._sender()
        now = now_s()
        if who == d["buyer"] and d["status"] == OPEN:
            self._refund_all(d, now, "buyer cancelled before acceptance")
            return
        if who == d["seller"] and d["status"] in (OPEN, ACCEPTED, VERIFYING):
            self._refund_all(d, now, "seller declined")
            return
        raise Exception("You cannot cancel this deal now")

    @gl.public.write
    def check_handover(self, deal_id: int) -> None:
        # Buyer or seller asks validators to read RDAP + DNS now.
        d = self._deal(deal_id)
        who = self._sender()
        if who not in (d["buyer"], d["seller"]):
            raise Exception("Only buyer or seller can request a check")
        if d["status"] not in (ACCEPTED, VERIFYING):
            raise Exception("Deal must be ACCEPTED or VERIFYING to check")
        now = now_s()
        if d["status"] == ACCEPTED and now >= int(d["deadline"]):
            raise Exception("Deadline passed before the handover was seen; buyer can refund")
        if d["status"] == VERIFYING and now > int(d["deadline"]) + int(d["hold_seconds"]) + VERIFY_GRACE_SECONDS:
            raise Exception("Verification grace is over; buyer can refund")

        st = self._consensus_state(d["domain"])
        res = decide(st, d["mode"], d["target_registrar_id"], d["target_ns"], int(d["accepted_at"]))
        self._record_check(d, now, "handover", res["verdict"], res["reasons"], st)

        if res["verdict"] == UNREADABLE:
            self._log(d, "check UNREADABLE (no state change)", now)
            self._put(d)
            return

        if res["verdict"] == NO_MATCH:
            if d["status"] == VERIFYING:
                d["status"] = ACCEPTED
                d["first_match_at"] = 0
                self._log(d, "hold RESET: handover no longer matches", now)
            else:
                self._log(d, "check NO_MATCH", now)
            self._put(d)
            return

        if d["status"] == ACCEPTED:
            d["status"] = VERIFYING
            d["first_match_at"] = now
            self._log(d, "first MATCH, hold started", now)
            self._put(d)
            return

        if now - int(d["first_match_at"]) < int(d["hold_seconds"]):
            left = int(d["hold_seconds"]) - (now - int(d["first_match_at"]))
            self._log(d, "MATCH again, hold still running (" + str(left) + "s left)", now)
            self._put(d)
            return

        self._hold_complete(d, now, "two matching checks " + str(now - int(d["first_match_at"])) + "s apart")

    @gl.public.write
    def retention_check(self, deal_id: int) -> None:
        # Buyer may check at any time during retention (to catch a revert early).
        # Seller may check once retention has ended (to collect the last 20%).
        d = self._deal(deal_id)
        if d["status"] != RETAINED:
            raise Exception("Deal is not in retention")
        who = self._sender()
        now = now_s()
        ended = now >= int(d["retained_until"])
        if who == d["seller"]:
            if not ended:
                raise Exception("Retention has not ended yet")
        elif who != d["buyer"]:
            raise Exception("Only buyer or seller can request a retention check")

        st = self._consensus_state(d["domain"])
        v = classify_retention(st, d["mode"], d["target_registrar_id"], d["target_ns"],
                               d["baseline_registrar_id"], d["baseline_ns"])
        self._record_check(d, now, "retention", v, [], st)

        if v == UNREADABLE:
            self._log(d, "retention check UNREADABLE (no state change)", now)
            self._put(d)
            return
        if v == REVERTED:
            amount = self._locked(d)
            self._close(d, CLAWED_BACK, now)
            self._put(d)
            self._pay_buyer(d, amount, now, "domain moved back to the seller's original setup")
            self._put(d)
            return
        if not ended:
            self._log(d, "retention check " + v + " (retention still running)", now)
            self._put(d)
            return
        if v == MATCH:
            self._release_all(d, now, "retention ended, domain still on target")
            return
        # CHANGED after retention: not the seller's original setup, not the target.
        if d["arbiter"] != "":
            d["status"] = RETENTION_DISPUTED
            d["disputed_at"] = now
            self._log(d, "retention CHANGED: arbiter decides the retained part", now)
            self._put(d)
            return
        self._release_all(d, now, "retention ended; setup changed but not back to the seller's")

    @gl.public.write
    def confirm_received(self, deal_id: int) -> None:
        # Buyer can release everything still locked, at any stage.
        d = self._deal(deal_id)
        if self._sender() != d["buyer"]:
            raise Exception("Only the buyer can confirm")
        if d["status"] not in (OPEN, ACCEPTED, VERIFYING, DISPUTED, RETAINED, RETENTION_DISPUTED):
            raise Exception("Deal is closed")
        self._release_all(d, now_s(), "buyer confirmed")

    @gl.public.write
    def refund_after_deadline(self, deal_id: int) -> None:
        d = self._deal(deal_id)
        if self._sender() != d["buyer"]:
            raise Exception("Only the buyer can claim a refund")
        now = now_s()
        if d["status"] in (OPEN, ACCEPTED):
            if now < int(d["deadline"]):
                raise Exception("Deadline not reached yet")
            self._refund_all(d, now, "deadline passed without a verified handover")
            return
        if d["status"] == VERIFYING:
            if now <= int(d["deadline"]) + int(d["hold_seconds"]) + VERIFY_GRACE_SECONDS:
                raise Exception("A hold is running; refund opens after the grace period")
            self._refund_all(d, now, "hold was never completed")
            return
        raise Exception("Deal cannot be refunded now")

    # ---------- optional arbiter ----------

    @gl.public.write
    def dispute(self, deal_id: int, reason: str) -> None:
        # Only while the hold runs, and only if an arbiter was chosen at open.
        d = self._deal(deal_id)
        if self._sender() != d["buyer"]:
            raise Exception("Only the buyer can dispute")
        if d["arbiter"] == "":
            raise Exception("This deal has no arbiter; disputes are off")
        if d["status"] != VERIFYING:
            raise Exception("Disputes are only possible during the hold")
        now = now_s()
        d["status"] = DISPUTED
        d["disputed_at"] = now
        self._log(d, "DISPUTED by buyer: " + str(reason)[:200], now)
        self._put(d)

    @gl.public.write
    def arbiter_decide(self, deal_id: int, release_to_seller: bool) -> None:
        # Decides whatever is still locked (full price in DISPUTED,
        # the retained 20% in RETENTION_DISPUTED).
        d = self._deal(deal_id)
        if d["status"] not in (DISPUTED, RETENTION_DISPUTED):
            raise Exception("Deal is not disputed")
        if self._sender() != d["arbiter"]:
            raise Exception("Only the arbiter can decide")
        now = now_s()
        if bool(release_to_seller):
            self._release_all(d, now, "arbiter decision")
        elif d["status"] == RETENTION_DISPUTED:
            amount = self._locked(d)
            self._close(d, CLAWED_BACK, now)
            self._put(d)
            self._pay_buyer(d, amount, now, "arbiter decision")
            self._put(d)
        else:
            self._refund_all(d, now, "arbiter decision")

    @gl.public.write
    def settle_stale_dispute(self, deal_id: int) -> None:
        # If the arbiter is silent, the public evidence (two matches) stands.
        d = self._deal(deal_id)
        if d["status"] not in (DISPUTED, RETENTION_DISPUTED):
            raise Exception("Deal is not disputed")
        if self._sender() not in (d["buyer"], d["seller"]):
            raise Exception("Only buyer or seller")
        now = now_s()
        if now < int(d["disputed_at"]) + ARBITER_TIMEOUT_SECONDS:
            raise Exception("Arbiter still has time")
        self._release_all(d, now, "arbiter timeout, evidence stands")

    # ---------- views ----------

    @gl.public.view
    def get_deal(self, deal_id: int) -> str:
        key = str(int(deal_id))
        if key not in self.deals:
            return json.dumps({"error": "No such deal"})
        return self.deals[key]

    @gl.public.view
    def get_lookup(self, domain: str) -> str:
        dom = str(domain).strip().lower().rstrip(".")
        if dom not in self.lookups:
            return json.dumps({"error": "No lookup yet; call lookup(domain) first"})
        return self.lookups[dom]

    @gl.public.view
    def get_stats(self) -> str:
        return json.dumps({
            "test_mode": bool(self.test_mode),
            "deals": int(self.next_id),
            "escrow_wei": str(int(self.escrow_total)),
            "paid_wei": str(int(self.paid_total)),
            "refunded_wei": str(int(self.refunded_total)),
            "min_hold_seconds": int(self.min_hold),
            "min_retention_seconds": int(self.min_retention),
            "retain_bps": RETAIN_BPS,
            "supported_tlds": sorted(RDAP_BASE.keys()),
        })
