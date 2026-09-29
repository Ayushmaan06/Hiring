-- serp_web ships dormant, decided 2026-08-28 by the tool owner.
--
-- The adapter is built and tested (IMPLEMENTATION.md 2.1) but is not part of the
-- delivered product. The product is the loop the owner stated: JD -> SERP query ->
-- LinkedIn profiles -> ranked UI. Open-web corroboration sits outside it.
--
-- Nothing calls the adapter -- it has no worker job and no UI entry point, only a CLI --
-- so this row is belt and braces: with it disabled, even running the CLI by hand gets
-- PolicyDenied. Turning it on is a deliberate act, the same shape as mode C.
--
-- To wake it: set enabled = true, clear disabled_reason, and read ARCHITECTURE.md 2.4
-- first -- that section is why a wildcard row is acceptable at all.

update source_policy
   set enabled = false,
       disabled_reason = 'Dormant by decision 2026-08-28: built, tested, outside the delivered product scope.'
 where domain = '*' and adapter = 'serp_web';
