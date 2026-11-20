# Studio test plan (studio.genlayer.com)

You need two Studio accounts:
- **A** = the buyer, with at least 2 GEN
- **B** = the seller

In Studio, the Value field only takes whole numbers, so use 1 GEN.

The test domain is `example.com`. Its records are public and stable, so we use them as the "target". This shows the full success path without needing to own a domain.

1. **Deploy.** Deploy `domain_handover.py`. The constructor has no arguments.
2. **Lookup.** Call `lookup("example.com")`, then `get_lookup("example.com")`. Note the `registrar_id` and `rdap_ns`.
3. **Open a deal.** As **A**, with Value 1, call `open_deal` with these arguments:
   - domain = `example.com`
   - seller = B's address
   - mode = `PUSH`
   - target_registrar_id = the `registrar_id` from step 2
   - target_nameservers = the two `rdap_ns` names, comma separated
   - deadline_hours = `24`
   - hold_seconds = `300`
   - arbiter = empty
4. **Check the deal.** Call `get_deal(0)`. The status should be `OPEN`, and `get_stats` should show 1 GEN in escrow.
5. **Accept.** As **B**, call `accept_deal(0)`. The status becomes `ACCEPTED`.
6. **First check.** As **B**, call `check_handover(0)`. The status becomes `VERIFYING` and the hold starts.
7. **Second check.** Wait at least 5 minutes, then as **B** call `check_handover(0)` again. The status becomes `PAID`, and B's balance goes up by 1 GEN.
8. **Mismatch test.** As **A**, with Value 1, call `open_deal` with the same domain but target_nameservers = `ns1.wrong.com,ns2.wrong.com`. As **B**, call `accept_deal(1)` and then `check_handover(1)`. The status stays `ACCEPTED`, and the check shows the reason "nameservers do not match".
9. **Refund test.** As **B**, call `cancel_deal(1)`. The status becomes `REFUNDED`, and A gets the 1 GEN back.

Take screenshots of steps 2, 6, 7, 8 and 9 to use as evidence.
