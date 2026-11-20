# Studio test plan (studio.genlayer.com)

You need two Studio accounts:
- **A** = the buyer
- **B** = the seller

In Studio, the Value field only takes whole numbers, so use 1 GEN. 80% of 1 GEN is 0.8 GEN, and 20% is 0.2 GEN.

## Test contract (`test_mode = true`)

1. **Deploy.** Deploy `domain_handover.py` with `test_mode` ticked (true).
2. **Lookup.** Call `lookup("example.com")`, then `get_lookup("example.com")`.
3. **Open a deal.** As **A**, with Value 1, call `open_deal` with these arguments:
   - domain = `example.com`
   - seller = B's address
   - mode = `PUSH`
   - target_registrar_id = `376`
   - target_nameservers = `elliott.ns.cloudflare.com,hera.ns.cloudflare.com`
   - deadline_hours = `24`
   - hold_seconds = `300`
   - retention_seconds = `300`
   - arbiter = empty
4. **Accept.** As **B**, call `accept_deal(0)`. The validators record the baseline.
5. **First check.** As **B**, call `check_handover(0)`. The status becomes `VERIFYING`.
6. **Second check.** Wait 5 minutes, then as **B** call `check_handover(0)`. The status becomes `RETAINED`, and B receives 0.8 GEN.
7. **Retention check.** Wait 5 minutes, then as **B** call `retention_check(0)`. The status becomes `PAID`, and B receives the last 0.2 GEN.
8. **Mismatch test.** As **A**, with Value 1, call `open_deal` with target_nameservers = `ns1.wrong.com,ns2.wrong.com`. As **B**, call `accept_deal(1)` and then `check_handover(1)`. The result is `NO_MATCH` and the status stays `ACCEPTED`.
9. **Refund test.** As **B**, call `cancel_deal(1)`. The status becomes `REFUNDED`.

## Live contract (`test_mode = false`)

10. **Deploy.** Deploy a second copy with `test_mode` unticked (false).
11. **Short hold refused.** As **A**, with Value 1, call `open_deal` as in step 3, keeping hold_seconds = `300`. It is refused with "hold_seconds must be 86400..".

This shows the live contract does not allow demo settings.
